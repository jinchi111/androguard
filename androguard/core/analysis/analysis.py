import re
import collections
from operator import itemgetter
import time
import warnings
from androguard.core.androconf import is_ascii_problem, load_api_specific_resource_module
from androguard.core.bytecodes import dvm
import logging
from androguard.core import bytecode, mutf8
import networkx as nx
from enum import IntEnum

log = logging.getLogger("androguard.analysis")

BasicOPCODES = set()
for i in dvm.BRANCH_DVM_OPCODES:
    p = re.compile(i)
    for op, items in dvm.DALVIK_OPCODES_FORMAT.items():
        if p.match(items[1][0]):
            BasicOPCODES.add(op)

# BasicOPCODESo = []
# for i in dvm.BRANCH_DVM_OPCODES:
#     BasicOPCODESo.append(re.compile(i))

class REF_TYPE(IntEnum):
    """
    Stores the opcodes for the type of usage in an XREF.

    Used in :class:`ClassAnalysis` to store the type of reference to the class.
    """
    REF_NEW_INSTANCE = 0x22
    REF_CLASS_USAGE = 0x1c
    INVOKE_VIRTUAL = 0x6e
    INVOKE_SUPER = 0x6f
    INVOKE_DIRECT = 0x70
    INVOKE_STATIC = 0x71
    INVOKE_INTERFACE = 0x72
    INVOKE_VIRTUAL_RANGE = 0x74
    INVOKE_SUPER_RANGE = 0x75
    INVOKE_DIRECT_RANGE = 0x76
    INVOKE_STATIC_RANGE = 0x77
    INVOKE_INTERFACE_RANGE = 0x78


class DVMBasicBlock:
    """
    A simple basic block of a dalvik method.

    A basic block consists of a series of :class:`~androguard.core.bytecodes.dvm.Instruction`
    which are not interrupted by branch or jump instructions such as `goto`, `if`, `throw`, `return`, `switch` etc.
    """
    def __init__(self, start, vm, method, context):
        self.__vm = vm
        self.method = method
        self.context = context

        self.last_length = 0
        self.nb_instructions = 0

        self.fathers = []
        self.childs = []

        self.start = start
        self.end = self.start

        self.special_ins = {}

        self.name = mutf8.MUTF8String.join([self.method.get_name(), b'-BB@', hex(self.start).encode()])
        self.exception_analysis = None

        self.notes = []

        self.__cached_instructions = None

    def get_notes(self):
        return self.notes

    def set_notes(self, value):
        self.notes = [value]

    def add_note(self, note):
        self.notes.append(note)

    def clear_notes(self):
        self.notes = []

    def get_instructions(self):
        """
        Get all instructions from a basic block.

        :returns: Return all instructions in the current basic block
        """
        idx = 0
        for i in self.method.get_instructions():
            if self.start <= idx < self.end:
                yield i
            idx += i.get_length()

    def get_nb_instructions(self):
        return self.nb_instructions

    def get_method(self):
        """
        Returns the originiating method

        :return: the method
        :rtype: androguard.core.bytecodes.dvm.EncodedMethod
        """
        return self.method

    def get_name(self):
        return self.name

    def get_start(self):
        """
        Get the starting offset of this basic block

        :return: starting offset
        :rtype: int
        """
        return self.start

    def get_end(self):
        """
        Get the end offset of this basic block

        :return: end offset
        :rtype: int
        """
        return self.end

    def get_last(self):
        """
        Get the last instruction in the basic block

        :return: androguard.core.bytecodes.dvm.Instruction
        """
        return list(self.get_instructions())[-1]

    def get_next(self):
        """
        Get next basic blocks

        :returns: a list of the next basic blocks
        :rtype: DVMBasicBlock
        """
        return self.childs

    def get_prev(self):
        """
        Get previous basic blocks

        :returns: a list of the previous basic blocks
        :rtype: DVMBasicBlock
        """
        return self.fathers

    def set_fathers(self, f):
        self.fathers.append(f)

    def get_last_length(self):
        return self.last_length

    def set_childs(self, values):
        # print self, self.start, self.end, values
        if not values:
            next_block = self.context.get_basic_block(self.end + 1)
            if next_block is not None:
                self.childs.append((self.end - self.get_last_length(), self.end,
                                    next_block))
        else:
            for i in values:
                if i != -1:
                    next_block = self.context.get_basic_block(i)
                    if next_block is not None:
                        self.childs.append((self.end - self.get_last_length(),
                                            i, next_block))

        for c in self.childs:
            if c[2] is not None:
                c[2].set_fathers((c[1], c[0], self))

    def push(self, i):
        self.nb_instructions += 1
        idx = self.end
        self.last_length = i.get_length()
        self.end += self.last_length

        op_value = i.get_op_value()

        if op_value == 0x26 or (0x2b <= op_value <= 0x2c):
            code = self.method.get_code().get_bc()
            self.special_ins[idx] = code.get_ins_off(idx + i.get_ref_off() * 2)

    def get_special_ins(self, idx):
        """
        Return the associated instruction to a specific instruction (for example a packed/sparse switch)

        :param idx: the index of the instruction

        :rtype: None or an Instruction
        """
        if idx in self.special_ins:
            return self.special_ins[idx]
        else:
            return None

    def get_exception_analysis(self):
        return self.exception_analysis

    def set_exception_analysis(self, exception_analysis):
        self.exception_analysis = exception_analysis

    def show(self):
        print("{}: {:04x} - {:04x}".format(self.get_name(), self.get_start(), self.get_end()))
        for note in self.get_notes():
            print(note)
        print('=' * 20)


class BasicBlocks:
    """
    This class represents all basic blocks of a method.

    It is a collection of many :class:`DVMBasicBlock`.
    """
    def __init__(self, _vm):
        """

        :param androguard.core.bytecodes.dvm.DalvikVMFormat _vm:
        """
        self.__vm = _vm
        self.bb = []

    def push(self, bb):
        """
        Adds another basic block to the collection

        :param DVBMBasicBlock bb: the DVMBasicBlock to add
        """
        self.bb.append(bb)

    def pop(self, idx):
        return self.bb.pop(idx)

    def get_basic_block(self, idx):
        for i in self.bb:
            if i.get_start() <= idx < i.get_end():
                return i
        return None

    def __len__(self):
        return len(self.bb)

    def __iter__(self):
        """
        :returns: yields each basic block (:class:`DVMBasicBlock` object)
        """
        yield from self.bb

    def __getitem__(self, item):
        """
        Get the basic block at the index

        :param item: index
        :return: The basic block
        :rtype: DVMBasicBlock
        """
        return self.bb[item]

    def gets(self):
        """
        :returns: a list of basic blocks (:class:`DVMBasicBlock` objects)
        """
        return self.bb

    # Alias for legacy programs
    get = __iter__
    get_basic_block_pos = __getitem__


class ExceptionAnalysis:
    def __init__(self, exception, bb):
        self.start = exception[0]
        self.end = exception[1]

        self.exceptions = exception[2:]

        for i in self.exceptions:
            i.append(bb.get_basic_block(i[1]))

    def show_buff(self):
        buff = "{:x}:{:x}\n".format(self.start, self.end)

        for i in self.exceptions:
            if i[2] is None:
                buff += "\t({} -> {:x} {})\n".format(i[0], i[1], i[2])
            else:
                buff += "\t({} -> {:x} {})\n".format(i[0], i[1], i[2].get_name())

        return buff[:-1]

    def get(self):
        d = {"start": self.start, "end": self.end, "list": []}

        for i in self.exceptions:
            d["list"].append({"name": i[0], "idx": i[1], "bb": i[2].get_name()})

        return d


class Exceptions:
    def __init__(self, _vm):
        self.__vm = _vm
        self.exceptions = []

    def add(self, exceptions, basic_blocks):
        for i in exceptions:
            self.exceptions.append(ExceptionAnalysis(i, basic_blocks))

    def get_exception(self, addr_start, addr_end):
        for i in self.exceptions:
            if i.start >= addr_start and i.end <= addr_end:
                return i

            elif addr_end <= i.end and addr_start >= i.start:
                return i

        return None

    def gets(self):
        return self.exceptions

    def get(self):
        for i in self.exceptions:
            yield i


class MethodAnalysis:
    def __init__(self, vm, method):
        """
        This class analyses in details a method of a class/dex file
        It is a wrapper around a :class:`EncodedMethod` and enhances it
        by using multiple :class:`DVMBasicBlock` encapsulated in a :class:`BasicBlocks` object.

        :type vm: a :class:`DalvikVMFormat` object
        :type method: a :class:`EncodedMethod` object
        """
        self.__vm = vm
        self.method = method

        self.basic_blocks = BasicBlocks(self.__vm)
        self.exceptions = Exceptions(self.__vm)

        self.code = self.method.get_code()
        if self.code:
            self._create_basic_block()

    def _create_basic_block(self):
        current_basic = DVMBasicBlock(0, self.__vm, self.method, self.basic_blocks)
        self.basic_blocks.push(current_basic)

        bc = self.code.get_bc()
        l = []
        h = {}
        idx = 0

        log.debug("Parsing instructions for method at @{}".format(self.method.get_code_off()))
        for i in bc.get_instructions():
            if i.OP in BasicOPCODES:
                v = dvm.determineNext(i, idx, self.method)
                h[idx] = v
                l.extend(v)

            idx += i.get_length()

        log.debug("Parsing exceptions")
        excepts = dvm.determineException(self.__vm, self.method)
        for i in excepts:
            l.extend([i[0]])
            for handler in i[2:]:
                l.append(handler[1])

        log.debug("Creating basic blocks in method at @%s" % self.method.get_code_off())
        idx = 0
        for i in bc.get_instructions():
            # index is a destination
            if idx in l:
                if current_basic.get_nb_instructions() != 0:
                    current_basic = DVMBasicBlock(current_basic.get_end(), self.__vm, self.method, self.basic_blocks)
                    self.basic_blocks.push(current_basic)

            current_basic.push(i)

            # index is a branch instruction
            if idx in h:
                current_basic = DVMBasicBlock(current_basic.get_end(), self.__vm, self.method, self.basic_blocks)
                self.basic_blocks.push(current_basic)

            idx += i.get_length()

        if current_basic.get_nb_instructions() == 0:
            self.basic_blocks.pop(-1)

        log.debug("Settings basic blocks childs")

        for i in self.basic_blocks.get():
            try:
                i.set_childs(h[i.end - i.get_last_length()])
            except KeyError:
                i.set_childs([])

        log.debug("Creating exceptions")

        # Create exceptions
        self.exceptions.add(excepts, self.basic_blocks)

        for i in self.basic_blocks.get():
            # setup exception by basic block
            i.set_exception_analysis(self.exceptions.get_exception(i.start, i.end - 1))

    def get_basic_blocks(self):
        """
        Returns the :class:`BasicBlocks` generated for this method.
        The :class:`BasicBlocks` can be used to get a control flow graph (CFG) of the method.

        :rtype: a :class:`BasicBlocks` object
        """
        return self.basic_blocks

    def get_length(self):
        """
        :rtype: an integer which is the length of the code
        """
        return self.code.get_length() if self.code else 0

    def get_vm(self):
        return self.__vm

    def get_method(self):
        return self.method

    def show(self):
        """
        Prints the content of this method to stdout.

        This will print the method signature and the decompiled code.
        """
        args, ret = self.method.get_descriptor()[1:].split(")")
        if self.code:
            # We patch the descriptor here and add the registers, if code is available
            args = args.split(" ")

            reg_len = self.code.get_registers_size()
            nb_args = len(args)

            start_reg = reg_len - nb_args
            args = ["{} v{}".format(a, start_reg + i) for i, a in enumerate(args)]

        print("METHOD {} {} {} ({}){}".format(
              self.method.get_class_name(),
              self.method.get_access_flags_string(),
              self.method.get_name(),
              ", ".join(args), ret))
        bytecode.PrettyShow(self, self.basic_blocks.gets(), self.method.notes)

    def __repr__(self):
        return "<analysis.MethodAnalysis {}>".format(self.method)


class StringAnalysis:
    """
    StringAnalysis contains the XREFs of a string.

    As Strings are only used as a source, they only contain
    the XREF_FROM set, i.e. where the string is used.

    This Array stores the information in which method the String is used.
    """
    def __init__(self, value):
        """

        :param str value: the original string value
        """
        self.value = value
        self.orig_value = value
        self.xreffrom = set()

    def add_xref_from(self, classobj, methodobj, off):
        """
        Adds a xref from the given method to this string

        :param ClassAnalysis classobj:
        :param androguard.core.bytecodes.dvm.EncodedMethod methodobj:
        :param int off: offset in the bytecode of the call
        """
        self.xreffrom.add((classobj, methodobj, off))

    def get_xref_from(self, withoffset=False):
        """
        Returns a list of xrefs accessing the String.

        The list contains tuples of the originating class and methods,
        where the class is represented as a :class:`ClassAnalysis`,
        while the method is a :class:`androguard.core.bytecodes.dvm.EncodedMethod`.
        """
        if withoffset:
            return self.xreffrom
        return set(map(itemgetter(slice(0, 2)), self.xreffrom))

    def set_value(self, value):
        """
        Overwrite the current value of the String with a new value.
        The original value is not lost and can still be retrieved using :meth:`get_orig_value`.

        :param str value: new string value
        """
        self.value = value

    def get_value(self):
        """
        Return the (possible overwritten) value of the String

        :return: the value of the string
        """
        return self.value

    def get_orig_value(self):
        """
        Return the original, read only, value of the String

        :return: the original value
        """
        return self.orig_value

    def is_overwritten(self):
        """
        Returns True if the string was overwritten
        :return:
        """
        return self.orig_value != self.value

    def __str__(self):
        data = "XREFto for string %s in\n" % repr(self.get_value())
        for ref_class, ref_method in self.xreffrom:
            data += "{}:{}\n".format(ref_class.get_vm_class().get_name(), ref_method)
        return data

    def __repr__(self):
        # TODO should remove all chars that are not pleasent. e.g. newlines
        if len(self.get_value()) > 20:
            s = "'{}'...".format(self.get_value()[:20])
        else:
            s = "'{}'".format(self.get_value())
        return "<analysis.StringAnalysis {}>".format(s)


class MethodClassAnalysis:
    def __init__(self, method):
        """
        MethodClassAnalysis contains the XREFs for a given method.

        Both referneces to other methods (XREF_TO) as well as methods calling
        this method (XREF_FROM) are saved.

        :param androguard.core.bytecodes.dvm.EncodedMethod method: the DVM Method object
        """
        self.method = method
        self.xrefto = set()
        self.xreffrom = set()

        # Reserved for further use
        self.apilist = None

    @property
    def name(self):
        """Returns the name of this method"""
        return self.method.get_name()

    @property
    def descriptor(self):
        """Returns the type descriptor for this method"""
        return self.method.get_descriptor()

    @property
    def access(self):
        """Returns the access flags to the method as a string"""
        return self.method.get_access_flags_string()

    @property
    def class_name(self):
        """Returns the name of the class of this method"""
        return self.method.class_name

    @property
    def full_name(self):
        """Returns classname + name + descriptor, separated by spaces (no access flags)"""
        return self.method.full_name

    def add_xref_to(self, classobj, methodobj, offset):
        """
        Add a crossreference to another method
        (this method calls another method)

        :param classobj: :class:`~ClassAnalysis`
        :param methodobj:  :class:`~androguard.core.bytecodes.dvm.EncodedMethod`
        :param offset: integer where in the method the call happens
        """
        self.xrefto.add((classobj, methodobj, offset))

    def add_xref_from(self, classobj, methodobj, offset):
        """
        Add a crossrefernece from another method
        (this method is called by another method)

        :param classobj: :class:`~ClassAnalysis`
        :param methodobj:  :class:`~androguard.core.bytecodes.dvm.EncodedMethod`
        :param offset: integer where in the method the call happens
        """
        self.xreffrom.add((classobj, methodobj, offset))

    def get_xref_from(self):
        """
        Returns a list of tuples containing the class, method and offset of
        the call, from where this object was called.

        The list of tuples has the form:
        (:class:`~ClassAnalysis`,
        :class:`~androguard.core.bytecodes.dvm.EncodedMethod` or
        :class:`~ExternalMethod`, :class:`int`)
        """
        return self.xreffrom

    def get_xref_to(self):
        """
        Returns a list of tuples containing the class, method and offset of
        the call, which are called by this method.

        The list of tuples has the form:
        (:class:`~ClassAnalysis`,
        :class:`~androguard.core.bytecodes.dvm.EncodedMethod` or
        :class:`~ExternalMethod`, :class:`int`)
        """
        return self.xrefto

    def is_external(self):
        """
        Returns True if the underlying method is external

        :rtype: boolean
        """
        return isinstance(self.method, ExternalMethod)

    def is_android_api(self):
        """
        Returns True if the method seems to be an Android API method.

        This method might be not very precise unless an list of known API methods
        is given.

        :return: boolean
        """
        if not self.is_external():
            # Method must be external to be an API
            return False

        # Packages found at https://developer.android.com/reference/packages.html
        api_candidates = ["Landroid/", "Lcom/android/internal/util", "Ldalvik/", "Ljava/", "Ljavax/", "Lorg/apache/",
                          "Lorg/json/", "Lorg/w3c/dom/", "Lorg/xml/sax", "Lorg/xmlpull/v1/", "Ljunit/"]

        if self.apilist:
            # FIXME: This will not work... need to introduce a name for lookup (like EncodedMethod.__str__ but without
            # the offset! Such a name is also needed for the lookup in permissions
            return self.method.get_name() in self.apilist
        else:
            for candidate in api_candidates:
                if self.method.get_class_name().startswith(candidate):
                    return True

        return False

    def get_method(self):
        """
        Return the `EncodedMethod` object that relates to this object
        :return: `dvm.EncodedMethod`
        """
        return self.method

    def __str__(self):
        data = "XREFto for %s\n" % self.method
        for ref_class, ref_method, offset in self.xrefto:
            data += "in\n"
            data += "{}:{} @0x{:x}\n".format(ref_class.get_vm_class().get_name(), ref_method, offset)

        data += "XREFFrom for %s\n" % self.method
        for ref_class, ref_method, offset in self.xreffrom:
            data += "in\n"
            data += "{}:{} @0x{:x}\n".format(ref_class.get_vm_class().get_name(), ref_method, offset)

        return data

    def __repr__(self):
        return "<analysis.MethodClassAnalysis {}{}>".format(self.method,
               " EXTERNAL" if isinstance(self.method, ExternalMethod) else "")


class FieldClassAnalysis:
    def __init__(self, field):
        """
        FieldClassAnalysis contains the XREFs for a class field.

        Instead of using XREF_FROM/XREF_TO, this object has methods for READ and
        WRITE access to the field.

        That means, that it will show you, where the field is read or written.

        :param androguard.core.bytecodes.dvm.EncodedField field: `dvm.EncodedField`
        """
        self.field = field
        self.xrefread = set()
        self.xrefwrite = set()

    @property
    def name(self):
        return self.field.get_name()

    def add_xref_read(self, classobj, methodobj, offset):
        """
        :param ClassAnalysis classobj:
        :param androguard.core.bytecodes.dvm.EncodedMethod methodobj:
        :param int offset: offset in the bytecode
        """
        self.xrefread.add((classobj, methodobj, offset))

    def add_xref_write(self, classobj, methodobj, offset):
        """
        :param ClassAnalysis classobj:
        :param androguard.core.bytecodes.dvm.EncodedMethod methodobj:
        :param int offset: offset in the bytecode
        """
        self.xrefwrite.add((classobj, methodobj, offset))

    def get_xref_read(self, withoffset=False):
        """
        Returns a list of xrefs where the field is read.

        The list contains tuples of the originating class and methods,
        where the class is represented as a :class:`ClassAnalysis`,
        while the method is a :class:`androguard.core.bytecodes.dvm.EncodedMethod`.

        :param bool withoffset: return the xrefs including the offset
        """
        if withoffset:
            return self.xrefread
        # Legacy option, might be removed in the future
        return set(map(itemgetter(slice(0, 2)), self.xrefread))

    def get_xref_write(self, withoffset=False):
        """
        Returns a list of xrefs where the field is written to.

        The list contains tuples of the originating class and methods,
        where the class is represented as a :class:`ClassAnalysis`,
        while the method is a :class:`androguard.core.bytecodes.dvm.EncodedMethod`.

        :param bool withoffset: return the xrefs including the offset
        """
        if withoffset:
            return self.xrefwrite
        # Legacy option, might be removed in the future
        return set(map(itemgetter(slice(0, 2)), self.xrefwrite))

    def get_field(self):
        return self.field

    def __str__(self):
        data = "XREFRead for %s\n" % self.field
        for ref_class, ref_method, off in self.xrefread:
            data += "in\n"
            data += "{}:{} @{}\n".format(ref_class.get_vm_class().get_name(), ref_method, off)

        data += "XREFWrite for %s\n" % self.field
        for ref_class, ref_method, off in self.xrefwrite:
            data += "in\n"
            data += "{}:{} @{}\n".format(ref_class.get_vm_class().get_name(), ref_method, off)

        return data

    def __repr__(self):
        return "<analysis.FieldClassAnalysis {}->{}>".format(self.field.class_name, self.field.name)


class ExternalClass:
    def __init__(self, name):
        """
        The ExternalClass is used for all classes that are not defined in the
        DEX file, thus are external classes.

        :param name: Name of the external class
        """
        self.name = name
        self.methods = {}

    def get_methods(self):
        """
        Return the stored methods for this external class
        :return:
        """
        return self.methods.values()

    def GetMethod(self, name, descriptor):
        """
        .. deprecated:: 3.1.0
            Use :meth:`get_method` instead.

        """
        warnings.warn("deprecated, use get_method instead. This function might be removed in a later release!", DeprecationWarning)
        return self.get_method(name, descriptor)

    def get_method(self, name, descriptor):
        """
        Get the method by name and descriptor,
        or create a new one if the requested method does not exists.

        :param name: method name
        :param descriptor: method descriptor, for example `'(I)V'`
        :return: :class:`ExternalMethod`
        """
        key = name + mutf8.MUTF8String.join(descriptor)
        if key not in self.methods:
            self.methods[key] = ExternalMethod(self.name, name, descriptor)

        return self.methods[key]

    def get_name(self):
        """
        Returns the name of the ExternalClass object
        """
        return self.name

    def __repr__(self):
        return "<analysis.ExternalClass {}>".format(self.name)


class ExternalMethod:
    def __init__(self, class_name, name, descriptor):
        self.class_name = class_name
        self.name = name
        self.descriptor = descriptor

    def get_name(self):
        return self.name

    def get_class_name(self):
        return self.class_name

    def get_descriptor(self):
        return mutf8.MUTF8String.join(self.descriptor)

    @property
    def full_name(self):
        """Returns classname + name + descriptor, separated by spaces (no access flags)"""
        return self.class_name + " " + self.name + " " + self.get_descriptor()

    @property
    def permission_api_name(self):
        """Returns a name which can be used to look up in the permission maps"""
        return self.class_name + "-" + self.name + "-" + self.get_descriptor()

    def get_access_flags_string(self):
        # TODO can we assume that external methods are always public?
        # they can also be static...
        # or constructor...
        return ""

    def __str__(self):
        return "{}->{}{}".format(self.class_name.__str__(), self.name.__str__(), str(mutf8.MUTF8String.join(self.descriptor)))

    def __repr__(self):
        return "<analysis.ExternalMethod {}>".format(self.__str__())


class ClassAnalysis:
    def __init__(self, classobj):
        """
        ClassAnalysis contains the XREFs from a given Class.
        It is also used to wrap :class:`~androguard.core.bytecode.dvm.ClassDefItem`, which
        contain the actual class content like bytecode.

        Also external classes will generate xrefs, obviously only XREF_FROM are
        shown for external classes.

        :param classobj: class:`~androguard.core.bytecode.dvm.ClassDefItem` or :class:`ExternalClass`
        """

        # Automatically decide if the class is external or not
        self.external = isinstance(classobj, ExternalClass)

        self.orig_class = classobj
        self._inherits_methods = {}
        self._methods = {}
        self._fields = {}

        self.xrefto = collections.defaultdict(set)
        self.xreffrom = collections.defaultdict(set)

        # Reserved for further use
        self.apilist = None

    @property
    def implements(self):
        """
        Get a list of interfaces which are implemented by this class

        :return: a list of Interface names
        """
        if self.is_external():
            return []

        return self.orig_class.get_interfaces()

    @property
    def extends(self):
        """
        Return the parent class

        For external classes, this is not sure, thus we return always Object (which is the parent of all classes)

        :return: a string of the parent class name
        """
        if self.is_external():
            return "Ljava/lang/Object;"

        return self.orig_class.get_superclassname()

    @property
    def name(self):
        """
        Return the class name

        :return:
        """
        return self.orig_class.get_name()

    def is_external(self):
        """
        Tests wheather this class is an external class

        :return: True if the Class is external, False otherwise
        """
        return self.external

    def is_android_api(self):
        """
        Tries to guess if the current class is an Android API class.

        This might be not very precise unless an apilist is given, with classes that
        are in fact known APIs.
        Such a list might be generated by using the android.jar files.

        :return: boolean
        """

        # Packages found at https://developer.android.com/reference/packages.html
        api_candidates = ["Landroid/", "Lcom/android/internal/util", "Ldalvik/", "Ljava/", "Ljavax/", "Lorg/apache/",
                          "Lorg/json/", "Lorg/w3c/dom/", "Lorg/xml/sax", "Lorg/xmlpull/v1/", "Ljunit/"]

        if not self.is_external():
            # API must be external
            return False

        if self.apilist:
            return self.orig_class.get_name() in self.apilist
        else:
            for candidate in api_candidates:
                if self.orig_class.get_name().startswith(candidate):
                    return True

        return False

    def get_methods(self):
        """
        Return all :class:`MethodClassAnalysis` objects of this class

        :rtype: Iterator[MethodClassAnalysis]
        """
        return list(self._methods.values())

    def get_fields(self):
        """
        Return all `FieldClassAnalysis` objects of this class
        """
        return self._fields.values()

    def get_nb_methods(self):
        """
        Get the number of methods in this class
        """
        return len(self._methods)

    def get_method_analysis(self, method):
        """
        Return the MethodClassAnalysis object for a given EncodedMethod

        :param method: :class:`EncodedMethod`
        :return: :class:`MethodClassAnalysis`
        """
        return self._methods.get(method)

    def get_field_analysis(self, field):
        return self._fields.get(field)

    def get_fake_method(self, name, descriptor):
        """
        Search for the given method name and descriptor
        and return a fake (ExternalMethod) if required.

        :param name: name of the method
        :param descriptor: descriptor of the method, for example `'(I I I)V'`
        :return: :class:`ExternalMethod`
        """
        if self.external:
            # An external class can only generate the methods on demand
            return self.orig_class.get_method(name, descriptor)

        # We are searching an unknown method in this class
        # It could be something that the class herits
        key = name + mutf8.MUTF8String.join(descriptor)
        if key not in self._inherits_methods:
            self._inherits_methods[key] = ExternalMethod(self.orig_class.get_name(), name, descriptor)
        return self._inherits_methods[key]

    def add_field_xref_read(self, method, classobj, field, off):
        """
        Add a Field Read to this class

        :param androguard.core.bytecodes.dvm.EncodedMethod method:
        :param ClassAnalysis classobj:
        :param str field:
        :param int off:
        :return:
        """
        if field not in self._fields:
            self._fields[field] = FieldClassAnalysis(field)
        self._fields[field].add_xref_read(classobj, method, off)

    def add_field_xref_write(self, method, classobj, field, off):
        """
        Add a Field Write to this class in a given method

        :param androguard.core.bytecodes.dvm.EncodedMethod method:
        :param ClassAnalysis classobj:
        :param str field:
        :param int off:
        :return:
        """
        if field not in self._fields:
            self._fields[field] = FieldClassAnalysis(field)
        self._fields[field].add_xref_write(classobj, method, off)

    def add_method_xref_to(self, method1, classobj, method2, offset):
        if method1 not in self._methods:
            self._methods[method1] = MethodClassAnalysis(method1)
        self._methods[method1].add_xref_to(classobj, method2, offset)

    def add_method_xref_from(self, method1, classobj, method2, offset):
        if method1 not in self._methods:
            self._methods[method1] = MethodClassAnalysis(method1)
        self._methods[method1].add_xref_from(classobj, method2, offset)

    def AddXrefTo(self, ref_kind, classobj, methodobj, offset):
        """
        Creates a crossreference to another class.
        XrefTo means, that the current class calls another class.
        The current class should also be contained in the another class' XrefFrom list.

        :param REF_TYPE ref_kind: type of call
        :param classobj: :class:`ClassAnalysis` object to link
        :param methodobj:
        :param offset: Offset in the Methods Bytecode, where the call happens
        :return:
        """
        self.xrefto[classobj].add((ref_kind, methodobj, offset))

    def AddXrefFrom(self, ref_kind, classobj, methodobj, offset):
        """
        Creates a crossreference from this class.
        XrefFrom means, that the current class is called by another class.

        :param REF_TYPE ref_kind: type of call
        :param classobj: :class:`ClassAnalysis` object to link
        :param methodobj:
        :param offset: Offset in the methods bytecode, where the call happens
        :return:
        """
        self.xreffrom[classobj].add((ref_kind, methodobj, offset))

    def get_xref_from(self):
        """
        Returns a dictionary of all classes calling the current class.
        This dictionary contains also information from which method the class is accessed.

        .. note:: this method might contains wrong information about class usage!

        The dictionary contains the classes as keys (stored as :class:`ClassAnalysis`)
        and has a tuple as values, where the first item is the ref_kind (which is an Enum of type :class:`REF_TYPE`),
        the second one is the method in which the class is called (either :class:`ExternalMethod` if external or
        :class:`androguard.core.bytecodes.dvm.EncodedMethod` if internal)
        and the third the offset in the method where the call is originating.

        example::
            # dx is an Analysis object
            for cls in dx.find_classes('.*some/name.*'):
                print("Found class {} in Analysis".format(cls.name)
                for caller, refs in cls.get_xref_from().items():
                    print("  called from {}".format(caller.name))
                    for ref_kind, ref_method, ref_offset in refs:
                        print("    in method {} {}".format(ref_kind, ref_method))

        """
        return self.xreffrom

    def get_xref_to(self):
        """
        Returns a dictionary of all classes which are called by the current class.
        This dictionary contains also information about the method which is called.

        .. note:: this method might contains wrong information about class usage!

        The dictionary contains the classes as keys (stored as :class:`ClassAnalysis`)
        and has a tuple as values, where the first item is the ref_kind (which is an Enum of type :class:`REF_TYPE`),
        the second one is the method called (either :class:`ExternalMethod` if external or
        :class:`androguard.core.bytecodes.dvm.EncodedMethod` if internal)
        and the third the offset in the method where the call is originating.

        example::
            # dx is an Analysis object
            for cls in dx.find_classes('.*some/name.*'):
                print("Found class {} in Analysis".format(cls.name)
                for calling, refs in cls.get_xref_from().items():
                    print("  calling class {}".format(calling.name))
                    for ref_kind, ref_method, ref_offset in refs:
                        print("    calling method {} {}".format(ref_kind, ref_method))
        """
        return self.xrefto

    def get_vm_class(self):
        return self.orig_class

    def __repr__(self):
        return "<analysis.ClassAnalysis {}{}>".format(self.orig_class.get_name(),
                " EXTERNAL" if isinstance(self.orig_class, ExternalClass) else "")

    def __str__(self):
        # Print only instantiation from other classes here
        # TODO also method xref and field xref should be printed?
        data = "XREFto for %s\n" % self.orig_class
        for ref_class in self.xrefto:
            data += str(ref_class.get_vm_class().get_name()) + " "
            data += "in\n"
            for ref_kind, ref_method, ref_offset in self.xrefto[ref_class]:
                data += "%d %s 0x%x\n" % (ref_kind, ref_method, ref_offset)

            data += "\n"

        data += "XREFFrom for %s\n" % self.orig_class
        for ref_class in self.xreffrom:
            data += str(ref_class.get_vm_class().get_name()) + " "
            data += "in\n"
            for ref_kind, ref_method, ref_offset in self.xreffrom[ref_class]:
                data += "%d %s 0x%x\n" % (ref_kind, ref_method, ref_offset)

            data += "\n"

        return data


class Analysis:
    def __init__(self, vm=None):
        """
        Analysis Object

        The Analysis contains a lot of information about (multiple) DalvikVMFormat objects
        Features are for example XREFs between Classes, Methods, Fields and Strings.

        Multiple DalvikVMFormat Objects can be added using the function `add`

        XREFs are created for:
        * classes (`ClassAnalysis`)
        * methods (`MethodClassAnalysis`)
        * strings (`StringAnalyis`)
        * fields (`FieldClassAnalysis`)

        :param vm: inital DalvikVMFormat object (default None)
        """

        # Contains DalvikVMFormat objects
        self.vms = []
        # A dict of {classname: ClassAnalysis}, populated on add(vm)
        self.classes = {}
        # A dict of {string: StringAnalysis}, populated on create_xref()
        self.strings = {}
        # A dict of {EncodedMethod: MethodAnalysis}, populated on add(vm)
        self.methods = {}

        if vm:
            self.add(vm)

    def add(self, vm):
        """
        Add a DalvikVMFormat to this Analysis

        :param vm: :class:`dvm.DalvikVMFormat` to add to this Analysis
        """
        self.vms.append(vm)
        log.info("Adding DEX file version {}".format(vm.version))
        for current_class in vm.get_classes():
            self.classes[current_class.get_name()] = ClassAnalysis(current_class)

        for method in vm.get_methods():
            self.methods[method] = MethodAnalysis(vm, method)

    def _get_all_classes(self):
        """
        Returns all Class objects of all VMs in this Analysis
        Used by create_xref().
        """
        for vm in self.vms:
            for current_class in vm.get_classes():
                yield current_class

    def create_xref(self):
        """
        Create Class, Method, String and Field crossreferences
        for all classes in the Analysis.

        If you are using multiple DEX files, this function must
        be called when all DEX files are added.
        If you call the function after every DEX file, the
        crossreferences might be wrong!
        """
        log.debug("Creating Crossreferences (XREF)")
        tic = time.time()

        # TODO on concurrent runs, we probably need to clean up first,
        # or check that we do not write garbage.

        # TODO multiprocessing
        # One reason why multiprocessing is hard to implement is the creation of
        # the external classes and methods. This must be synchronized.
        for c in self._get_all_classes():
            self._create_xref(c)

        # TODO: After we collected all the information, we should add field and
        # string xrefs to each MethodClassAnalysis

        log.info("End of creating cross references (XREF) "
                 "run time: {:0d}min {:02d}s".format(*divmod(int(time.time() - tic), 60)))

    def _create_xref(self, current_class):
        """
        Create the xref for `current_class`

        There are four steps involved in getting the xrefs:
        * Xrefs for class instantiation and static class usage
        *       for method calls
        *       for string usage
        *       for field manipulation

        All these information are stored in the *Analysis Objects.

        Note that this might be quite slow, as all instructions are parsed.

        :param androguard.core.bytecodes.dvm.ClassDefItem current_class: The class to create xrefs for
        """
        cur_cls_name = current_class.get_name()

        log.debug("Creating XREF/DREF for class at @%s" % current_class.get_class_data_off())
        for current_method in current_class.get_methods():
            log.debug("Creating XREF for method at @%s" % current_method.get_code_off())

            for off, instruction in current_method.get_instructions_idx():
                op_value = instruction.get_op_value()

                # 1) check for class calls: const-class (0x1c), new-instance (0x22)
                if op_value in [0x1c, 0x22]:
                    idx_type = instruction.get_ref_kind()
                    # type_info is the string like 'Ljava/lang/Object;'
                    type_info = instruction.cm.vm.get_cm_type(idx_type).lstrip(b'[')
                    if type_info[0] != b'L':
                        # Need to make sure, that we get class types and not other types
                        continue

                    # Internal xref related to class manipulation
                    # FIXME should the xref really only set if the class is in self.classes? If an external class is added later, it will be added too!
                    # See https://github.com/androguard/androguard/blob/d720ebf2a9c8e2a28484f1c81fdddbc57e04c157/androguard/core/analysis/analysis.py#L806
                    # Before the check would go for internal classes only!
                    # FIXME: effectively ignoring calls to itself - do we want that?
                    if type_info == cur_cls_name:
                        continue

                    if type_info not in self.classes:
                        # Create new external class
                        self.classes[type_info] = ClassAnalysis(ExternalClass(type_info))

                    cur_cls = self.classes[cur_cls_name]
                    oth_cls = self.classes[type_info]

                    # FIXME: xref_to does not work here! current_method is wrong, as it is not the target!
                    cur_cls.AddXrefTo(REF_TYPE(op_value), oth_cls, current_method, off)
                    oth_cls.AddXrefFrom(REF_TYPE(op_value), cur_cls, current_method, off)

                # 2) check for method calls: invoke-* (0x6e ... 0x72), invoke-xxx/range (0x74 ... 0x78)
                elif (0x6e <= op_value <= 0x72) or (0x74 <= op_value <= 0x78):
                    idx_meth = instruction.get_ref_kind()
                    method_info = instruction.cm.vm.get_cm_method(idx_meth)
                    if not method_info:
                        log.warning("Could not get method_info for instruction at {} in method at @{}".format(off, current_method.get_code_off()))
                        continue

                    class_info = method_info[0].lstrip(b'[')
                    if class_info[0] != b'L':
                        # Need to make sure, that we get class types and not other types
                        # If another type, like int is used, we simply skip it.
                        continue

                    method_item = None
                    # TODO: should create get_method_descriptor inside Analysis,
                    # otherwise we need to search in all DalvikVMFormat objects
                    # for the corrent method
                    for vm in self.vms:
                        method_item = vm.get_method_descriptor(class_info, method_info[1], mutf8.MUTF8String.join(method_info[2]))
                        if method_item:
                            break

                    if not method_item:
                        # Seems to be an external class, create it first
                        # Beware: if not all DEX files are loaded at the time create_xref runs
                        # you will run into problems!
                        if class_info not in self.classes:
                            self.classes[class_info] = ClassAnalysis(ExternalClass(class_info))
                        method_item = self.classes[class_info].get_fake_method(method_info[1], method_info[2])

                    self.classes[cur_cls_name].add_method_xref_to(current_method, self.classes[class_info], method_item, off)
                    self.classes[class_info].add_method_xref_from(method_item, self.classes[cur_cls_name], current_method, off)

                    # Internal xref related to class manipulation
                    if class_info in self.classes and class_info != cur_cls_name:
                        self.classes[cur_cls_name].AddXrefTo(REF_TYPE(op_value), self.classes[class_info], method_item, off)
                        self.classes[class_info].AddXrefFrom(REF_TYPE(op_value), self.classes[cur_cls_name], current_method, off)

                # 3) check for string usage: const-string (0x1a), const-string/jumbo (0x1b)
                elif 0x1a <= op_value <= 0x1b:
                    string_value = instruction.cm.vm.get_cm_string(instruction.get_ref_kind())
                    if string_value not in self.strings:
                        self.strings[string_value] = StringAnalysis(string_value)

                    self.strings[string_value].add_xref_from(self.classes[cur_cls_name], current_method, off)

                # TODO maybe we should add a step 3a) here and check for all const fields. You can then xref for integers etc!
                # But: This does not work, as const fields are usually optimized internally to const calls...

                # 4) check for field usage: i*op (0x52 ... 0x5f), s*op (0x60 ... 0x6d)
                elif 0x52 <= op_value <= 0x6d:
                    idx_field = instruction.get_ref_kind()
                    field_info = instruction.cm.vm.get_cm_field(idx_field)
                    field_item = instruction.cm.vm.get_field_descriptor(field_info[0], field_info[2], field_info[1])
                    if not field_item:
                        continue

                    if (0x52 <= op_value <= 0x58) or (0x60 <= op_value <= 0x66):
                        # read access to a field
                        self.classes[cur_cls_name].add_field_xref_read(current_method, self.classes[cur_cls_name], field_item, off)
                    else:
                        # write access to a field
                        self.classes[cur_cls_name].add_field_xref_write(current_method, self.classes[cur_cls_name], field_item, off)

    def get_method(self, method):
        """
        Get the :class:`MethodAnalysis` object for a given :class:`EncodedMethod`.
        This Analysis object is used to enhance EncodedMethods.

        :param method: :class:`EncodedMethod` to search for
        :return: :class:`MethodAnalysis` object for the given method, or None if method was not found
        """
        if method in self.methods:
            return self.methods[method]
        else:
            return None

    def get_method_by_name(self, class_name, method_name, method_descriptor):
        """
        Search for a :class:`EncodedMethod` in all classes in this analysis

        :param class_name: name of the class, for example 'Ljava/lang/Object;'
        :param method_name: name of the method, for example 'onCreate'
        :param method_descriptor: descriptor, for example '(I I Ljava/lang/String)V
        :return: :class:`EncodedMethod` or None if method was not found
        """
        if class_name in self.classes:
            for method in self.classes[class_name].get_vm_class().get_methods():
                if method.get_name() == method_name and method.get_descriptor() == method_descriptor:
                    return method
        return None

    def get_method_analysis(self, method):
        """
        Returns the crossreferencing object for a given Method.

        Beware: the similar named function :meth:`~get_method()` will return
        a :class:`MethodAnalysis` object, while this function returns a :class:`MethodClassAnalysis` object!

        This Method will only work after a run of :meth:`~create_xref()`

        :param method: :class:`EncodedMethod`
        :return: :class:`MethodClassAnalysis` for the given method or None, if method was not found
        """
        class_analysis = self.get_class_analysis(method.get_class_name())
        if class_analysis:
            return class_analysis.get_method_analysis(method)
        return None

    def get_method_analysis_by_name(self, class_name, method_name, method_descriptor):
        """
        Returns the crossreferencing object for a given method.

        This function is similar to :meth:`~get_method_analysis`, with the difference
        that you can look up the Method by name

        :param class_name: name of the class, for example `'Ljava/lang/Object;'`
        :param method_name: name of the method, for example `'onCreate'`
        :param method_descriptor: method descriptor, for example `'(I I)V'`
        :return: :class:`MethodClassAnalysis`
        """
        method = self.get_method_by_name(class_name, method_name, method_descriptor)
        if method:
            return self.get_method_analysis(method)
        return None

    def get_field_analysis(self, field):
        """
        Get the FieldAnalysis for a given fieldname

        :param field: TODO
        :return: :class:`FieldClassAnalysis`
        """
        class_analysis = self.get_class_analysis(field.get_class_name())
        if class_analysis:
            return class_analysis.get_field_analysis(field)
        return None

    def is_class_present(self, class_name):
        """
        Checks if a given class name is part of this Analysis.

        :param class_name: classname like 'Ljava/lang/Object;' (including L and ;)
        :return: True if class was found, False otherwise
        """
        return class_name in self.classes

    def get_class_analysis(self, class_name):
        """
        Returns the :class:`ClassAnalysis` object for a given classname.

        :param class_name: classname like 'Ljava/lang/Object;' (including L and ;)
        :return: :class:`ClassAnalysis`
        """
        return self.classes.get(class_name)

    def get_external_classes(self):
        """
        Returns all external classes, that means all classes that are not
        defined in the given set of `DalvikVMObjects`.

        :rtype: Iterator[ClassAnalysis]
        """
        for cls in self.classes.values():
            if cls.is_external():
                yield cls

    def get_internal_classes(self):
        """
        Returns all external classes, that means all classes that are
        defined in the given set of :class:`~DalvikVMFormat`.

        :rtype: Iterator[ClassAnalysis]
        """
        for cls in self.classes.values():
            if not cls.is_external():
                yield cls

    def get_strings_analysis(self):
        """
        Returns a dictionary of strings and their corresponding :class:`StringAnalysis`

        :rtype: dict
        """
        return self.strings

    def get_strings(self):
        """
        Returns a list of :class:`StringAnalysis` objects

        :rtype: Iterator[StringAnalysis]
        """
        return self.strings.values()

    def get_classes(self):
        """
        Returns a list of :class:`ClassAnalysis` objects

        Returns both internal and external classes (if any)

        :rtype: Iterator[ClassAnalysis]
        """
        return self.classes.values()

    def get_methods(self):
        """
        Returns a list of `MethodClassAnalysis` objects

        """
        for c in self.classes.values():
            for m in c.get_methods():
                yield m

    def get_fields(self):
        """
        Returns a list of `FieldClassAnalysis` objects

        :rtype: Iterator[FieldClassAnalysis]
        """
        for c in self.classes.values():
            for f in c.get_fields():
                yield f

    def find_classes(self, name=".*", no_external=False):
        """
        Find classes by name, using regular expression
        This method will return all ClassAnalysis Object that match the name of
        the class.

        :param name: regular expression for class name (default ".*")
        :param no_external: Remove external classes from the output (default False)
        :rtype: Iterator[ClassAnalysis]
        """
        name = bytes(mutf8.MUTF8String.from_str(name))
        for cname, c in self.classes.items():
            if no_external and isinstance(c.get_vm_class(), ExternalClass):
                continue
            if re.match(name, cname):
                yield c

    def find_methods(self, classname=".*", methodname=".*", descriptor=".*",
            accessflags=".*", no_external=False):
        """
        Find a method by name using regular expression.
        This method will return all MethodClassAnalysis objects, which match the
        classname, methodname, descriptor and accessflags of the method.

        :param classname: regular expression for the classname
        :param methodname: regular expression for the method name
        :param descriptor: regular expression for the descriptor
        :param accessflags: regular expression for the accessflags
        :param no_external: Remove external method from the output (default False)
        :rtype: Iterator[MethodClassAnalysis]
        """
        classname = bytes(mutf8.MUTF8String.from_str(classname))
        methodname = bytes(mutf8.MUTF8String.from_str(methodname))
        descriptor = bytes(mutf8.MUTF8String.from_str(descriptor))
        for cname, c in self.classes.items():
            if re.match(classname, cname):
                for m in c.get_methods():
                    z = m.get_method()
                    # TODO is it even possible that an internal class has
                    # external methods? Maybe we should check for ExternalClass
                    # instead...
                    if no_external and isinstance(z, ExternalMethod):
                        continue
                    if re.match(methodname, z.get_name()) and \
                       re.match(descriptor, z.get_descriptor()) and \
                       re.match(accessflags, z.get_access_flags_string()):
                        yield m

    def find_strings(self, string=".*"):
        """
        Find strings by regex

        :param string: regular expression for the string to search for
        :rtype: Iterator[StringAnalysis]
        """
        string = bytes(mutf8.MUTF8String.from_str(string))
        for s, sa in self.strings.items():
            if re.match(string, s):
                yield sa

    def find_fields(self, classname=".*", fieldname=".*", fieldtype=".*", accessflags=".*"):
        """
        find fields by regex

        :param classname: regular expression of the classname
        :param fieldname: regular expression of the fieldname
        :param fieldtype: regular expression of the fieldtype
        :param accessflags: regular expression of the access flags
        :rtype: Iterator[FieldClassAnalysis]
        """
        classname = bytes(mutf8.MUTF8String.from_str(classname))
        fieldname = bytes(mutf8.MUTF8String.from_str(fieldname))
        fieldtype = bytes(mutf8.MUTF8String.from_str(fieldtype))
        for cname, c in self.classes.items():
            if re.match(classname, cname):
                for f in c.get_fields():
                    z = f.get_field()
                    if re.match(fieldname, z.get_name()) and \
                       re.match(fieldtype, z.get_descriptor()) and \
                       re.match(accessflags, z.get_access_flags_string()):
                        yield f

    def __repr__(self):
        return "<analysis.Analysis VMs: {}, Classes: {}, Strings: {}>".format(len(self.vms), len(self.classes), len(self.strings))

    def get_call_graph(self, classname=".*", methodname=".*", descriptor=".*",
                       accessflags=".*", no_isolated=False, entry_points=[]):
        """
        Generate a directed graph based on the methods found by the filters applied.
        The filters are the same as in
        :meth:`~androguard.core.analaysis.analaysis.Analysis.find_methods`

        A networkx.MultiDiGraph is returned, containing all xrefs.
        That means a method which calls another method multiple times, will have multiple
        edges between them. Attached to the edge is the attribute `offset`, which gives
        the code offset inside the method of the call.

        Specifying filters will not remove the methods if they are called by some other method.

        The callgraph will check for both directions of edges. Thus, if you specify a single class
        as input, it will contain all classes which are called by this class (xref_to),
        as well as all methods who calls the specified one (xref_from).

        Each node will contain the following meta information as attribute:

        * external: is the method external or not (boolean)
        * entrypoint: is the method a known entry point (boolean)
        * native: is the method a native method by signature (boolean)
        * public: is the method declared public (boolean)
        * static: is the method declared static (boolean)
        * vm: An ID of the DEX file where this method is declared or 0 if external (signed int)
        * codesize: size of code of the method or zero if external (int)

        :param classname: regular expression of the classname (default: ".*")
        :param methodname: regular expression of the methodname (default: ".*")
        :param descriptor: regular expression of the descriptor (default: ".*")
        :param accessflags: regular expression of the access flags (default: ".*")
        :param no_isolated: remove isolated nodes from the graph, e.g. methods which do not call anything (default: False)
        :param entry_points: A list of classes that are marked as entry point

        :rtype: networkx.MultiDiGraph
        """

        def _add_node(G, method):
            """
            Wrapper to add methods to a graph without duplication

            method might be EncodedMethod or ExternalMethod
            """
            if method in G.node:
                return

            external = isinstance(method, ExternalMethod)

            G.add_node(method,
                       external=external,
                       entrypoint=method.get_class_name() in entry_points,
                       native="native" in method.get_access_flags_string(),
                       public="public" in method.get_access_flags_string(),
                       static="static" in method.get_access_flags_string(),
                       vm=hash(method.CM.vm) if not external else 0,
                       codesize=len(list(method.get_instructions())) if not external else 0,
                       )

        CG = nx.MultiDiGraph()

        # Note: If you create the CG from many classes at the same time, the drawing
        # will be a total mess... Hence it is recommended to reduce the number of nodes beforehand.
        # Obviously, you can always do this later at the costs of computational power.
        for m in self.find_methods(classname=classname, methodname=methodname,
                                   descriptor=descriptor, accessflags=accessflags):
            orig_method = m.get_method()
            log.info("Adding Method '{}' to callgraph".format(orig_method))

            if no_isolated and len(m.get_xref_to()) == 0 and len(m.get_xref_from()) == 0:
                log.info("Skipped {}, because if has no xrefs".format(orig_method))
                continue

            _add_node(CG, orig_method)

            for _, callee, offset in m.get_xref_to():
                _add_node(CG, callee)
                CG.add_edge(orig_method, callee, key=offset, offset=offset)

            for _, caller, offset in m.get_xref_from():
                # If _all_ methods are added to the CG, this will not make any difference.
                # But, if only a single class is chosen as a seed point, we require this information too!
                # This is particularly useful for external classes, as they do not have xref_to,
                # thus if an external class is chosen as starting point, it will generate an empty graph.
                _add_node(CG, caller)
                CG.add_edge(caller, orig_method, key=offset, offset=offset)

        return CG

    def create_ipython_exports(self):
        """
        .. warning:: this feature is experimental and is currently not enabled by default! Use with caution!

        Creates attributes for all classes, methods and fields on the Analysis object itself.
        This makes it easier to work with Analysis module in an iPython shell.

        Classes can be search by typing :code:`dx.CLASS_<tab>`, as each class is added via this attribute name.
        Each class will have all methods attached to it via :code:`dx.CLASS_Foobar.METHOD_<tab>`.
        Fields have a similar syntax: :code:`dx.CLASS_Foobar.FIELD_<tab>`.

        As Strings can contain nearly anything, use :meth:`find_strings` instead.

        * Each `CLASS_` item will return a :class:`~ClassAnalysis`
        * Each `METHOD_` item will return a :class:`~MethodClassAnalysis`
        * Each `FIELD_` item will return a :class:`~FieldClassAnalysis`
        """
        # TODO: it would be fun to have the classes organized like the packages. I.e. you could do dx.CLASS_xx.yyy.zzz
        for cls in self.get_classes():
            name = "CLASS_" + bytecode.FormatClassToPython(cls.name)
            if hasattr(self, name):
                log.warning("Already existing class {}!".format(name))
            setattr(self, name, cls)

            for meth in cls.get_methods():
                method_name = meth.name
                if method_name in ["<init>", "<clinit>"]:
                    _, method_name = bytecode.get_package_class_name(cls.name)

                # FIXME this naming schema is not very good... but to describe a method uniquely, we need all of it
                mname = "METH_" + method_name + "_" + bytecode.FormatDescriptorToPython(meth.access) + "_" + bytecode.FormatDescriptorToPython(meth.descriptor)
                if hasattr(cls, mname):
                    log.warning("already existing method: {} at class {}".format(mname, name))
                setattr(cls, mname, meth)

            # FIXME: syntetic classes produce problems here.
            # If the field name is the same in the parent as in the syntetic one, we can only add one!
            for field in cls.get_fields():
                mname = "FIELD_" + bytecode.FormatNameToPython(field.name)
                if hasattr(cls, mname):
                    log.warning("already existing field: {} at class {}".format(mname, name))
                setattr(cls, mname, field)

    def get_permissions(self, apilevel=None):
        """
        Returns the permissions and the API method based on the API level specified.
        This can be used to find usage of API methods which require a permission.
        Should be used in combination with an :class:`~androguard.core.bytecodes.apk.APK`.

        The returned permissions are a list, as some API methods require multiple permissions at once.

        The following example shows the usage and how to get the calling methods using XREF:

        example::
            from androguard.misc import AnalyzeAPK
            a, d, dx = AnalyzeAPK("somefile.apk")

            for meth, perm in dx.get_permissions(a.get_effective_target_sdk_version()):
                print("Using API method {} for permission {}".format(meth, perm))
                print("used in:")
                for _, m, _ in meth.get_xref_from():
                    print(m.full_name)

        ..note::
            This method might be unreliable and might not extract all used permissions.
            The permission mapping is based on [Axplorer](https://github.com/reddr/axplorer)
            and might be incomplete due to the nature of the extraction process.
            Unfortunately, there is no official API<->Permission mapping.

            The output of this method relies also on the set API level.
            If the wrong API level is used, the results might be wrong.

        :param apilevel: API level to load, or None for default
        :return: yields tuples of :class:`MethodClassAnalysis` (of the API method) and list of permission string
        """

        # TODO maybe have the API level loading in the __init__ method and pass the APK as well?
        permmap = load_api_specific_resource_module('api_permission_mappings', apilevel)
        if not permmap:
            raise ValueError("No permission mapping found! Is one available? "
                             "The requested API level was '{}'".format(apilevel))

        for cls in self.get_external_classes():
            for meth_analysis in cls.get_methods():
                meth = meth_analysis.get_method()
                if meth.permission_api_name in permmap:
                    yield meth_analysis, permmap[meth.permission_api_name]

    def get_permission_usage(self, permission, apilevel=None):
        """
        Find the usage of a permission inside the Analysis.

        example::
            from androguard.misc import AnalyzeAPK
            a, d, dx = AnalyzeAPK("somefile.apk")

            for meth in dx.get_permission_usage('android.permission.SEND_SMS', a.get_effective_target_sdk_version()):
                print("Using API method {}".format(meth))
                print("used in:")
                for _, m, _ in meth.get_xref_from():
                    print(m.full_name)

        .. note::
            The permission mappings might be incomplete! See also :meth:`get_permissions`.

        :param permission: the name of the android permission (usually 'android.permission.XXX')
        :param apilevel: the requested API level or None for default
        :return: yields :class:`MethodClassAnalysis` objects for all using API methods
        """

        # TODO maybe have the API level loading in the __init__ method and pass the APK as well?
        permmap = load_api_specific_resource_module('api_permission_mappings', apilevel)
        if not permmap:
            raise ValueError("No permission mapping found! Is one available? "
                             "The requested API level was '{}'".format(apilevel))

        apis = {k for k, v in permmap.items() if permission in v}
        if not apis:
            raise ValueError("No API methods could be found which use the permission. "
                             "Does the permission exists? You requested: '{}'".format(permission))

        for cls in self.get_external_classes():
            for meth_analysis in cls.get_methods():
                meth = meth_analysis.get_method()
                if meth.permission_api_name in apis:
                    yield meth_analysis


def is_ascii_obfuscation(vm):
    """
    Tests if any class inside a DalvikVMObject
    uses ASCII Obfuscation (e.g. UTF-8 Chars in Classnames)

    :param vm: `DalvikVMObject`
    :return: True if ascii obfuscation otherwise False
    """
    for classe in vm.get_classes():
        if is_ascii_problem(classe.get_name()):
            return True
        for method in classe.get_methods():
            if is_ascii_problem(method.get_name()):
                return True
    return False
