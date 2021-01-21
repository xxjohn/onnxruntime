# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

import json
import ort_flatbuffers_py.experimental.fbs as fbs

from abc import ABC, abstractmethod
from .types import value_name_to_typestr


def _create_op_key(domain: str, optype: str):
    return '{}:{}'.format(domain, optype)


class TypeUsageProcessor(ABC):
    '''
    Abstract base class for processors which implement operator specific logic to determine the type or types required.
    '''
    def __init__(self, domain: str, optype: str):
        self._name = _create_op_key(domain, optype)

    def name(self):
        return self._name

    def cpp_name(self):
        'Return a string that can be used as a unique name in a C++ #define.'
        return self._name.upper().replace('.', '_').replace(':', '_')

    @abstractmethod
    def process_node(self, node: fbs.Node, value_name_to_typeinfo: dict):
        pass

    def is_typed_registration_needed(self, type_in_registration):
        '''
        Given the string from a kernel registration, determine if the registration is required or not.
        :param type_in_registration: Type string from kernel registration
        :return: True is required. False if not.
        '''
        # Not all operators have typed registrations, so this is optionally implemented by derived classes
        raise RuntimeError('Did not expect processor for {} to have typed registrations.'.format(self._name))

    @abstractmethod
    def get_cpp_defines(self):
        '''
        Get the C++ #defines for this operator's required types
        :return: List with any applicable #defines. One line per entry.
        '''
        pass

    @abstractmethod
    def to_config_entry(self):
        pass

    @abstractmethod
    def from_config_entry(self, entry: str):
        pass


class DefaultTypeUsageProcessor(TypeUsageProcessor):
    '''
    Operator processor which tracks the types used for selected input/s and/or output/s.
    '''

    def __init__(self, domain: str, optype: str, inputs: [int] = [0], outputs: [int] = []):
        super().__init__(domain, optype)
        self._input_types = {}
        self._output_types = {}

        for i in inputs:
            self._input_types[i] = set()

        for o in outputs:
            self._output_types[o] = set()

    def process_node(self, node: fbs.Node, value_name_to_typeinfo: dict):
        for i in self._input_types.keys():
            if i >= node.InputsLength():
                raise RuntimeError('Node has {} inputs. Tracker for {} incorrectly configured as it requires {}.'
                                   .format(node.InputsLength(), self.name(), i))

            type_str = value_name_to_typestr(node.Inputs(i), value_name_to_typeinfo)
            self._input_types[i].add(type_str)

        for o in self._output_types.keys():
            if o >= node.OutputsLength():
                raise RuntimeError('Node has {} outputs. Tracker for {} incorrectly configured as it requires {}.'
                                   .format(node.OutputsLength(), self.name(), o))

            type_str = value_name_to_typestr(node.Outputs(o), value_name_to_typeinfo)
            self._output_types[o].add(type_str)

    def is_typed_registration_needed(self, type_in_registration: str):
        if 0 not in self._input_types.keys():
            raise RuntimeError('Expected typed registration to be done using type from input 0.')

        return type_in_registration in self._input_types[0]

    def get_cpp_defines(self):
        defines = []
        for i in sorted(self._input_types.keys()):
            if self._input_types[i]:
                defines.append('#define {}_INPUT{}_TYPES std::tuple<{}>'
                               .format(self.cpp_name(), i, ','.join(sorted(self._input_types[i]))))

        for o in sorted(self._output_types.keys()):
            if self._output_types[o]:
                defines.append('#define {}_OUTPUT{}_TYPES std::tuple<{}>'
                               .format(self.cpp_name(), o, ','.join(sorted(self._output_types[o]))))

        return defines

    def to_config_entry(self):
        aggregate_info = {'inputs': {}, 'outputs': {}}

        # filter out empty entries and output nicely sorted
        for i in sorted(self._input_types.keys()):
            if self._input_types[i]:
                aggregate_info['inputs'][i] = sorted(self._input_types[i])

        for o in sorted(self._output_types.keys()):
            if self._output_types[o]:
                aggregate_info['outputs'][o] = sorted(self._output_types[o])

        if not aggregate_info['inputs']:
            aggregate_info.pop('inputs')
        if not aggregate_info['outputs']:
            aggregate_info.pop('outputs')

        entry = json.dumps(aggregate_info) if aggregate_info else None
        return entry

    def from_config_entry(self, entry: str):
        self._input_types.clear()
        self._output_types.clear()

        aggregate_info = json.loads(entry)
        if 'inputs' in aggregate_info:
            for i_str, values in aggregate_info['inputs'].items():
                self._input_types[int(i_str)] = set(values)

        if 'outputs' in aggregate_info:
            for o_str, values in aggregate_info['outputs'].items():
                self._output_types[int(o_str)] = set(values)


class OneHotProcessor(TypeUsageProcessor):
    'Processor for the OneHot operator'
    def __init__(self):
        super().__init__('ai.onnx', 'OneHot')
        self._triples = set()

    def process_node(self, node: fbs.Node, value_name_to_typeinfo: dict):
        type0 = value_name_to_typestr(node.Inputs(0), value_name_to_typeinfo)
        type1 = value_name_to_typestr(node.Inputs(1), value_name_to_typeinfo)
        type2 = value_name_to_typestr(node.Inputs(2), value_name_to_typeinfo)
        key = '{}_{}_{}'.format(type0, type1, type2)
        self._triples.add(key)

    def is_typed_registration_needed(self, type_in_registration):
        # the OneHot registration creates a triple from the 3 types involved
        return type_in_registration in self._triples

    def get_cpp_defines(self):
        # exclusion via registration so don't need to write any #defines
        return None

    def to_config_entry(self):
        if not self._triples:
            return None

        aggregate_info = {'custom': sorted(self._triples)}
        entry = json.dumps(aggregate_info)
        return entry

    def from_config_entry(self, entry: str):
        self._triples.clear()
        aggregate_info = json.loads(entry)
        if 'custom' in aggregate_info:
            self._triples = set(aggregate_info['custom'])


def _create_operator_type_usage_processors():
    '''
    Create a set of processors that determine the required types for all enabled operators.
    :return: Dictionary of operator key to processor. Key is 'domain:operator'.
    '''
    operator_processors = {}

    def add(processor):
        if processor.name() in operator_processors:
            raise RuntimeError('Duplicate processor for ' + processor.name())

        operator_processors[processor.name()] = processor

    # Starting with ops from:
    #   - the Office production models
    #   - Mobilenet + SSD Mobilenet + MobileBert
    #   - some known large kernels
    # Excludes ops where type reduction is meaningless e.g. current implementation only supports one type or is small
    default_processor_onnx_ops = ['Add', 'AveragePool', 'BatchNormalization', 'Clip', 'Concat', 'Conv',
                                  'DequantizeLinear', 'Div', 'Equal', 'Exp', 'Expand', 'Flatten',
                                  'Gemm', 'Greater', 'Less', 'MatMul', 'Max', 'Min', 'Mul',
                                  'NonMaxSuppression', 'NonZero', 'Pad', 'QLinearConv', 'Relu', 'Resize',
                                  'Sigmoid', 'Slice', 'Softmax', 'Split', 'Sub', 'Tile', 'TopK', 'Transpose']

    # TODO - review and add ML ops as needed
    # ML Op notes.
    #  CastMap: Switch on value type of input map type, and output type
    #  DictVectorizer: Templatized on key+value of input so need to handle like OneHot with custom processor
    #  LabelEncoder: Implementation switches on input and output types (only supports string and int64 in T1 and T2)
    #  LinearClassifier: Internal switch on input type and also switch on output type
    #  SVMClassifier: ditto
    #  TreeEnsembleClassifier: Templatized on input type and also switch on output type
    #  ZipMap: Switch on output type (derived from attributes)
    default_processor_onnxml_ops = []

    # FusedConv, FusedGemm and TransposeMatMul are float only so can be ignored
    internal_ops = ['QLinearAdd', 'QLinearMul']

    [add(DefaultTypeUsageProcessor('ai.onnx', op)) for op in default_processor_onnx_ops]
    [add(DefaultTypeUsageProcessor('ai.onnx.ml', op)) for op in default_processor_onnxml_ops]
    [add(DefaultTypeUsageProcessor('com.microsoft', op)) for op in internal_ops]

    #
    # Operators that require slightly different handling
    #
    add(DefaultTypeUsageProcessor('ai.onnx', 'Cast', inputs=[0], outputs=[0]))  # track input0 and output0

    # Gather and GatherElements have switching on both the data type (input0) and indices type (input1)
    add(DefaultTypeUsageProcessor('ai.onnx', 'Gather', inputs=[0, 1]))
    add(DefaultTypeUsageProcessor('ai.onnx', 'GatherElements', inputs=[0, 1]))

    # Pow dispatches on base and exponential types
    add(DefaultTypeUsageProcessor('ai.onnx', 'Pow', inputs=[0, 1]))

    # Random generator ops produce new data so we track the output type
    onnx_random_ops = ['RandomNormal', 'RandomNoarmalLike', 'RandomUniform', 'RandomUniformLike', 'Multinomial']
    [add(DefaultTypeUsageProcessor('ai.onnx', op, inputs=[], outputs=[0])) for op in onnx_random_ops]

    # we only support 'float' as input for QuantizeLinear so just track the output type
    add(DefaultTypeUsageProcessor('ai.onnx', 'QuantizeLinear', inputs=[], outputs=[0]))

    # OneHot concatenates type strings into a triple in the typed registration
    # e.g. float_int64_t_int64_t
    add(OneHotProcessor())

    return operator_processors


class OperatorTypeUsageManager:
    '''
    Class to manage the operator type usage processors.
    TODO: Currently the type tracking is not specific to a version of the operator.
    It's unclear how/where version specific logic could/should be added, and it would add significant complexity
    to track types on a per-version basis. Not clear there's enough benefit from doing so either.
    '''
    def __init__(self):
        self._all_operator_processors = _create_operator_type_usage_processors()  # all possible processors
        self._operator_processors = {}  # processors we have actually used.

    def _get_op_processor(self, key):
        processor = None
        if key in self._all_operator_processors:
            if key not in self._operator_processors:
                self._operator_processors[key] = self._all_operator_processors[key]

            processor = self._operator_processors[key]

        return processor

    def process_node(self, node: fbs.Node, value_name_to_typeinfo: dict):
        '''
        Process a Node and record info on the types used.
        :param node: Node from ORT model
        :param value_name_to_typeinfo: Map of value names to TypeInfo instances
        '''
        optype = node.OpType().decode()
        domain = node.Domain().decode() or 'ai.onnx'  # empty domain defaults to ai.onnx

        key = _create_op_key(domain, optype)
        op_processor = self._get_op_processor(key)
        if op_processor:
            op_processor.process_node(node, value_name_to_typeinfo)

    def is_typed_registration_needed(self, domain: str, optype: str, registration_type: str):
        '''
        Given the string from a kernel registration, determine if the registration is required or not.
        :param domain: Operator domain.
        :param optype: Operator type.
        :param registration_type: Type string from kernel registration
        :return: True is required. False if not.
        '''
        needed = True  # we keep the registration unless the per-operator processor says not to
        key = _create_op_key(domain, optype)
        if key in self._operator_processors:
            needed = self._operator_processors[key].is_typed_registration_needed(registration_type)

        return needed

    def get_cpp_defines(self):
        '''
        Get the C++ #defines that define the lists of types to enable for the operators we have type info for.
        :return: List of strings with one #define per entry
        '''
        defines = []
        for key in sorted(self._operator_processors.keys()):
            defines.extend(self._operator_processors[key].get_cpp_defines())

        return defines

    def get_config_entry(self, domain: str, optype: str):
        '''
        Get the config entry specifying the types for this operator.
        :param domain: Operator domain.
        :param optype: Operator type.
        :return: JSON string with type info if available, else None
        '''
        key = _create_op_key(domain, optype)
        config_str = None
        if key in self._operator_processors:
            config_str = self._operator_processors[key].to_config_entry()

        return config_str

    def restore_from_config_entry(self, domain: str, optype: str, config_entry: str):
        '''
        Restore the per-operator type information from a configuration file entry.
        :param domain: Operator domain.
        :param optype: Operator type.
        :param config_entry: JSON string with type info as created by get_config_entry
        '''
        key = _create_op_key(domain, optype)
        op_processor = self._get_op_processor(key)
        if op_processor:
            op_processor.from_config_entry(config_entry)

    def debug_dump(self):

        print('#defines that will be created:')
        for key in sorted(self._operator_processors.keys()):
            [print(define) for define in self._operator_processors[key].get_cpp_defines()]

        print('Config file type information that will be returned by get_config_entry:')
        for key in sorted(self._operator_processors.keys()):
            entry = self._operator_processors[key].to_config_entry()
            if entry:
                print('{} -> {}'.format(key, entry))

                # roundtrip test to validate that we can initialize the processor from the entry and get the
                # same values back
                self._operator_processors[key].from_config_entry(entry)
                assert(entry == self._operator_processors[key].to_config_entry())
