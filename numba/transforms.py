"""
This module provides a variety of transforms that transform the AST
into a final form ready for code generation.

Below follows an explanation and justification of the design of the main
compilation stages in numba.

We start with a Python AST, compiled from source code or decompiled from
bytecode using meta. We run the following transformations:

    1) Type inference

        Infer types of all expressions, and fix the types of all local
        variables. Local variable types are promoted (for instance float
        to double), but cannot change (e.g. string cannot be assigned to
        float).

        When the type inferencer cannot determine a type, such as when it
        calls a Python function or method that is not a Numba function, it
        assumes type object. Object variables may be coerced to and from
        most native types.

        The type inferencer inserts CoercionNode nodes that perform such
        coercions, as well as coercions between promotable native types.
        It also resolves the return type of many math functions called
        in the numpy, math and cmath modules.

        Each AST expression node has a Variable that holds the type of
        the expression, as well as any meta-data such as constant values
        that have been determined.

    2) Transform for loops

        Provides some transformations of for loops over arrays to loops
        over a range. Iteration over range/xrange is resolved at
        compilation time.

        What I would like to see is the generation of a RangeNode holding
        a ast.Compare and an iteration variable incrementing ast.BinOp.

    3) Low level specializations (LateSpecializer)

        This stage performs low-level specializations. For instance it
        resolves coercions to and from object into calls such as
        PyFloat_FromDouble, with a fallback to Py_BuildValue/PyArg_ParseTuple.

        This specializer also has the responsibility to ensure that new
        references are accounted for by refcounting ObjectTempNode nodes.
        This node absorbs the references and lets parent nodes borrow the
        reference. At function cleanup, it decrefs its value. In loops,
        it also decrefs any previous value, if set. Hence, these temporaries
        must be initialized to NULL.

        An object temporary is specific to one specific sub-expression, and
        they are not reused (like in Cython).

        It also rewrites object attribute access and method calls into
        PyObject_GetAttrString etc.

    4) Code generation

        Generate LLVM code from the transformed AST.

        This should be as minimal as possible, and should *not* contain
        blobs of code performing complex operations. Instead, complex
        operations should be broken down by AST transformers into
        fundamental operations that are already supported by the AST.

        This way we maximize code reuse, and make potential future additions
        of different code generation backends easier. This can be taken
        only so far, since low-level transformations must also tailor to
        limitations of the code generation backend, such as intrinsic LLVM
        calls or calls into libc. However, code reuse is especially convenient
        in the face of type coercions, which LLVM does not provide any
        leniency for.
"""


import ast
import copy
import opcode
import types
import ctypes
import __builtin__ as builtins

import numba
from numba import *
from numba import error
from .minivect import minierror, minitypes
from . import translate, utils, _numba_types as numba_types
from .symtab import Variable
from . import visitors, nodes, error, functions
from numba import stdio_util
from numba._numba_types import is_obj, promote_closest

import llvm.core
import numpy as np

logger = logging.getLogger(__name__)

class MathMixin(object):
    """
    Resolve calls to math functions.

    During type inference this produces MathNode nodes, and during
    final specialization it produces LLVMIntrinsicNode and MathCallNode
    nodes.
    """

    # sin(double), sinf(float), sinl(long double)
    libc_math_funcs = [
        'sin',
        'cos',
        'tan',
        'acos',
        'asin',
        'atan',
        'atan2',
        'sinh',
        'cosh',
        'tanh',
        'asinh',
        'acosh',
        'atanh',
        'expm1',
        'log2',
        'fabs',
        'pow',
        'erfc',
        'ceil',
        'rint',
        'round',
    ]

    def get_funcname(self, py_func):
        if py_func is np.abs:
            return 'abs'
        elif py_func is np.round:
            return 'round'

        return py_func.__name__

    def _is_intrinsic(self, py_func):
        "Whether the math function is available as an llvm intrinsic"
        intrinsic_name = 'INTR_' + self.get_funcname(py_func).upper()
        is_intrinsic = hasattr(llvm.core, intrinsic_name)
        return is_intrinsic

    def _is_math_function(self, func_args, py_func):
        if len(func_args) > 1 or py_func is None:
            return False

        type = func_args[0].variable.type
        is_intrinsic = self._is_intrinsic(py_func)
        is_math = self.get_funcname(py_func) in self.libc_math_funcs

        return (type.is_float or type.is_int) and (is_intrinsic or is_math)

    def _resolve_intrinsic(self, args, py_func, signature):
        return nodes.LLVMIntrinsicNode(signature, args, None, py_func)

    def math_suffix(self, name, type):
        if name == 'abs':
            name = 'fabs'

        if type.itemsize == 4:
            name += 'f' # sinf(float)
        elif type.itemsize == 16:
            name += 'l' # sinl(long double)
        return name

    def _resolve_libc_math(self, args, py_func, signature):
        arg_type = signature.args[0]
        name = self.math_suffix(self.get_funcname(py_func), arg_type)
        return nodes.MathCallNode(signature, args, llvm_func=None,
                                  py_func=py_func, name=name)

    def _resolve_math_call(self, call_node, py_func, coerce_to_input_type=True):
        "Resolve calls to math functions to llvm.log.f32() etc"
        # signature is a generic signature, build a correct one
        orig_type = type = call_node.args[0].variable.type
        if not type.is_float:
            type = double

        signature = minitypes.FunctionType(return_type=type, args=[type])
        result = nodes.MathNode(py_func, signature, call_node.args[0])
        if coerce_to_input_type:
            return nodes.CoercionNode(result, orig_type)
        else:
            return result

    def _binop_type(self, x, y):
        "Binary result type for math operations"
        x_type = x.variable.type
        y_type = y.variable.type
        dst_type = self.promote_types(x_type, y_type)
        type = dst_type
        if type.is_int:
            type = double

        signature = minitypes.FunctionType(return_type=type, args=[type, type])
        return dst_type, type, signature

    def pow(self, node, power, mod=None):
        name = 'pow'
        dst_type, pow_type, signature = self._binop_type(node, power)
        args = [node, power]
        if pow_type.is_float and mod is None:
            result = self._resolve_intrinsic(args, pow, signature)
        else:
            if mod is not None:
                args.append(mod)
            result = nodes.call_pyfunc(pow, args)

        return nodes.CoercionNode(result, dst_type)


class BuiltinResolverMixinBase(MathMixin):
    """
    Base class for mixins resolving calls to built-in functions.

    Methods called _resolve_<built-in name> are called to handle calls
    to the built-in of that name.
    """

    def _resolve_builtin_call(self, node, func):
        """
        Resolve an ast.Call() of a built-in function.

        Returns None if no specific transformation is applied.
        """
        resolver = getattr(self, '_resolve_' + func.__name__, None)
        if resolver is not None:
            # Pass in the first argument type
            argtype = None
            if len(node.args) >= 1:
                argtype = node.args[0].variable.type

            return resolver(func, node, argtype)

        return None

    def _resolve_builtin_call_or_object(self, node, func):
        """
        Resolve an ast.Call() of a built-in function, or call the built-in
        through the object layer otherwise.
        """
        result = self._resolve_builtin_call(node, func)
        if result is None:
            result = nodes.call_pyfunc(func, node.args)

        return result

    def _expect_n_args(self, func, node, n):
        if not isinstance(n, tuple):
            n = (n,)

        if len(node.args) not in n:
            expected = " or ".join(map(str, n))
            raise error.NumbaError(
                node, "builtin %s expects %s arguments" % (func.__name__,
                                                           expected))

class LateBuiltinResolverMixin(BuiltinResolverMixinBase):
    """
    Perform final low-level transformations such as abs(value) -> fabs(value)
    """

    def _resolve_abs(self, func, node, argtype):
        self._expect_n_args(func, node, 1)

        # TODO: generate efficient inline code
        if argtype.is_float:
            return self._resolve_math_call(node, abs)
        elif argtype.is_int:
            if argtype.signed:
                type = promote_closest(self.context, argtype, [long_, longlong])
                funcs = {long_: 'labs', longlong: 'llabs'}
                return self.function_cache.call(funcs[type], node.args[0])
            else:
                # abs() on unsigned integral value
                return node.args[0]

        return None

    def _resolve_round(self, func, node, argtype):
        self._expect_n_args(func, node, (1, 2))
        if len(node.args) == 1 and argtype.is_int:
            # round(myint) -> myint
            return node.args[0]
        elif self._is_math_function(node.args, round):
            # round() always returns a float
            return self._resolve_math_call(node, round,
                                           coerce_to_input_type=False)

        return None

    def _resolve_pow(self, func, node, argtype):
        self._expect_n_args(func, node, (2, 3))
        return self.pow(*node.args)


class TransformForIterable(visitors.NumbaTransformer):
    """
    This transforms for loops such as loops over 1D arrays:

            for value in my_array:
                ...

        into

            for i in my_array.shape[0]:
                value = my_array[i]
    """

    def __init__(self, context, func, ast, symtab, **kwds):
        super(TransformForIterable, self).__init__(context, func, ast, **kwds)
        self.symtab = symtab

    def visit_For(self, node):
        if node.iter.type.is_range:
            return node
        elif node.iter.type.is_array and node.iter.type.ndim == 1:
            # Convert 1D array iteration to for-range and indexing
            logger.debug(ast.dump(node))

            orig_target = node.target
            orig_iter = node.iter

            # replace node.target with a temporary
            target_name = orig_target.id + '.idx'
            target_temp = nodes.TempNode(minitypes.Py_ssize_t)
            node.target = target_temp.store()

            # replace node.iter
            call_func = ast.Name(id='range', ctx=ast.Load())
            call_func.type = numba_types.RangeType()
            shape_index = ast.Index(nodes.ConstNode(0, numba_types.Py_ssize_t))
            shape_index.type = numba_types.npy_intp
            stop = ast.Subscript(value=nodes.ShapeAttributeNode(orig_iter),
                                 slice=shape_index,
                                 ctx=ast.Load())
            stop.type = numba_types.intp
            stop.variable = Variable(stop.type)
            call_args = [nodes.ConstNode(0, numba_types.Py_ssize_t),
                         nodes.CoercionNode(stop, numba_types.Py_ssize_t),
                         nodes.ConstNode(1, numba_types.Py_ssize_t),]

            node.iter = ast.Call(func=call_func, args=call_args)
            node.iter.type = call_func.type

            node.index = target_temp.load()
            # add assignment to new target variable at the start of the body
            index = ast.Index(value=node.index)
            index.type = target_temp.type
            subscript = ast.Subscript(value=orig_iter,
                                      slice=index, ctx=ast.Load())
            subscript.type = orig_iter.variable.type.dtype
            subscript.variable = Variable(subscript.type)
            coercion = nodes.CoercionNode(subscript, orig_target.type)
            assign = ast.Assign(targets=[orig_target], value=subscript)

            node.body = [assign] + node.body

            return node
        else:
            raise error.NumbaError("Unsupported for loop pattern")

class ResolveCoercions(visitors.NumbaTransformer):

    def visit_CoercionNode(self, node):
        if not isinstance(node, nodes.CoercionNode):
            # CoercionNode.__new__ returns the node to be coerced if it doesn't
            # need coercion
            return node

        node_type = node.node.type
        dst_type = node.dst_type

        if self.nopython and is_obj(node_type):
            raise error.NumbaError(node, "Cannot coerce to or from object in "
                                         "nopython context")

        if is_obj(node.dst_type) and not is_obj(node_type):
            node = nodes.ObjectTempNode(nodes.CoerceToObject(
                    node.node, node.dst_type, name=node.name))
            return self.visit(node)
        elif is_obj(node_type) and not is_obj(node.dst_type):
            node = nodes.CoerceToNative(node.node, node.dst_type,
                                        name=node.name)
            return self.visit(node)

        if node.node.type == node.type:
            node = self.visit(node.node)
        else:
            self.generic_visit(node)

        return node

    def _get_int_conversion_func(self, type, funcs_dict):
        type = self.context.promote_types(type, long_)
        if type in funcs_dict:
            return funcs_dict[type]

        if type.itemsize == long_.itemsize:
            types = [ulong, long_]
        else:
            types = [ulonglong, longlong]

        return self._get_int_conversion_func(types[type.signed], funcs_dict)

    def visit_CoerceToObject(self, node):
        new_node = node

        node_type = node.node.type
        if node_type.is_numeric:
            cls = None
            if node_type.is_int:
                cls = self._get_int_conversion_func(node_type,
                                                    functions._from_long)
            elif node_type.is_float:
                cls = functions.PyFloat_FromDouble
            elif node_type.is_complex:
                  cls = functions.PyComplex_FromCComplex

            if cls:
                new_node = self.function_cache.call(cls.__name__, node.node)
        elif node_type.is_pointer and not node_type.is_string():
            # Create ctypes pointer object
            ctypes_pointer_type = node_type.to_ctypes()
            args = [nodes.CoercionNode(node.node, int64),
                    nodes.ObjectInjectNode(ctypes_pointer_type, object_)]
            new_node = nodes.call_pyfunc(ctypes.cast, args)

        self.generic_visit(new_node)
        return new_node

    def visit_CoerceToNative(self, node):
        """
        Try to perform fast coercion using e.g. PyLong_AsLong(), with a
        fallback to PyArg_ParseTuple().
        """
        new_node = None

        node_type = node.type
        if node_type.is_numeric:
            cls = None
            if node_type == size_t:
                node_type = ulonglong

            if node_type.is_int: # and not
                cls = self._get_int_conversion_func(node_type,
                                                    functions._as_long)
                if not node_type.signed or node_type == Py_ssize_t:
                    # PyLong_AsLong calls __int__, but
                    # PyLong_AsUnsignedLong doesn't...
                    node.node = nodes.call_pyfunc(long, [node.node])
            elif node_type.is_float:
                cls = functions.PyFloat_AsDouble
            # elif node_type.is_complex:
            #      cls = functions.PyComplex_AsCComplex

            if cls:
                # TODO: error checking!
                new_node = self.function_cache.call(cls.__name__, node.node)
        elif node_type.is_pointer:
            raise error.NumbaError(
                    "Obtaining pointers from objects is not yet supported")

        if new_node is None:
            # Create a tuple for PyArg_ParseTuple
            new_node = node
            new_node.node = ast.Tuple(elts=[node.node], ctx=ast.Load())
        else:
            # Fast coercion
            new_node = nodes.CoercionNode(new_node, node.type)

        if new_node is node:
            self.generic_visit(new_node)
        else:
            new_node = self.visit(new_node)

        return new_node

class LateSpecializer(ResolveCoercions, LateBuiltinResolverMixin):

    def visit_FunctionDef(self, node):
        self.generic_visit(node)

        ret_type = self.func_signature.return_type
        if ret_type.is_object or ret_type.is_array:
            # This will require some increfs, but allow it if people
            # use 'with python' later on. If 'with python' isn't used, a
            # return will issue the error
            #if self.nopython:
            #    raise error.NumbaError(
            #            node, "Function cannot return object in "
            #                  "nopython context")
            value = nodes.NULL_obj
        elif ret_type.is_void:
            value = None
        elif ret_type.is_float:
            value = nodes.ConstNode(float('nan'), type=ret_type)
        elif ret_type.is_int or ret_type.is_complex:
            value = nodes.ConstNode(0xbadbadbad, type=ret_type)
        else:
            value = None

        if value is not None:
            value = nodes.CoercionNode(value, dst_type=ret_type)

        node.error_return = ast.Return(value=value)
        return node

    def check_context(self, node):
        if self.nopython:
            raise error.NumbaError(node, "Cannot construct object in "
                                         "nopython context")

    def _print(self, value, dest=None):
        stdin, stdout, stderr = stdio_util.get_stdio_streams()
        stdout = stdio_util.get_stream_as_node(stdout)

        signature, lfunc = self.function_cache.function_by_name(
                                                'PyObject_CallMethod')
        if dest is None:
            dest = nodes.ObjectInjectNode(sys.stdout)

        value = self.function_cache.call("PyObject_Str", value)
        args = [dest, nodes.ConstNode("write"), nodes.ConstNode("O"), value]
        return nodes.NativeCallNode(signature, args, lfunc)

    def visit_Print(self, node):
        if self.nopython:
            raise error.NumbaError(node, "Cannot use print statement in "
                                         "nopython context")

        print_space = self._print(nodes.ObjectInjectNode(" "), node.dest)

        result = []
        for value in node.values:
            value = nodes.CoercionNode(value, object_, name="print_arg")
            result.append(self._print(value, node.dest))
            result.append(print_space)

        result.pop() # pop last space

        if node.nl:
            result.append(self._print(nodes.ObjectInjectNode("\n"), node.dest))

        return self.visitlist(result)

    def visit_Tuple(self, node):
        self.check_context(node)

        sig, lfunc = self.function_cache.function_by_name('PyTuple_Pack')
        objs = self.visitlist(nodes.CoercionNode.coerce(node.elts, object_))
        n = nodes.ConstNode(len(node.elts), minitypes.Py_ssize_t)
        args = [n] + objs
        new_node = nodes.NativeCallNode(sig, args, lfunc, name='tuple')
        new_node.type = numba_types.TupleType(size=len(node.elts))
        return nodes.ObjectTempNode(new_node)

    def visit_List(self, node):
        self.check_context(node)
        self.generic_visit(node)
        return nodes.ObjectTempNode(node)

    def visit_Dict(self, node):
        self.check_context(node)
        self.generic_visit(node)
        return nodes.ObjectTempNode(node)

    def visit_Call(self, node):
        func_type = node.func.type

        if func_type.is_builtin and not node.type.is_range:
            result = self._resolve_builtin_call_or_object(node, func_type.func)
            result =  self.visit(result)
            return result

        self.generic_visit(node)
        return node

    def visit_NativeCallNode(self, node):
        self.generic_visit(node)
        if is_obj(node.signature.return_type):
            if self.nopython:
                raise error.NumbaError(
                        node, "Cannot call function returning object in "
                              "nopython context")
            node = nodes.ObjectTempNode(node)
        return node

    def visit_ObjectCallNode(self, node):
        # self.generic_visit(node)
        assert node.function

        if self.nopython:
            raise error.NumbaError(node, "Cannot use object call in "
                                         "nopython context")

        node.function = self.visit(node.function)
        node.args_tuple = self.visit(node.args_tuple)
        node.kwargs_dict = self.visit(node.kwargs_dict)
        return nodes.ObjectTempNode(node)

    def visit_MathNode(self, math_node):
        "Translate a nodes.MathNode to an intrinsic or libc math call"
        args = [math_node.arg], math_node.py_func, math_node.signature
        if self._is_intrinsic(math_node.py_func):
            result = self._resolve_intrinsic(*args)
        else:
            result = self._resolve_libc_math(*args)

        return self.visit(result)

    def visit_Subscript(self, node):
        if isinstance(node.value, nodes.ArrayAttributeNode):
            if node.value.is_read_only and isinstance(node.ctx, ast.Store):
                raise error.NumbaError("Attempt to load read-only attribute")

        # logging.debug(ast.dump(node))
        self.generic_visit(node)

        node_type = node.value.type
        if node_type.is_object or (node_type.is_array and
                                   node.slice.type.is_object):
            # Array or object slicing
            if isinstance(node.ctx, ast.Load):
                result = self.function_cache.call('PyObject_GetItem',
                                                  node.value, node.slice)
                # print ast.dump(result)
                node = nodes.CoercionNode(result, dst_type=node.type)
                node = self.visit(node)
            else:
                # This is handled in visit_Assign
                pass
        elif (node.value.type.is_array and not node.type.is_array and
                  node.slice.type.is_int):
            # Array index with integer indices
            node = nodes.DataPointerNode(node.value, node.slice, node.ctx)

        return node

    def visit_ExtSlice(self, node):
        if node.type.is_object:
            return self.visit(ast.Tuple(elts=node.dims, ctx=ast.Load()))
        else:
            self.generic_visit(node)
            return node

    def visit_Index(self, node):
        return self.visit(node.value)

    def visit_Assign(self, node):
        target = node.targets[0]
        if (len(node.targets) == 1 and
                isinstance(target, ast.Subscript) and is_obj(target.type)):
            # Slice assignment / index assignment w/ objects
            # TODO: discount array indexing with dtype object
            target = self.visit(target)
            obj = target.value
            key = target.slice
            value = self.visit(node.value)
            call = self.function_cache.call('PyObject_SetItem',
                                            obj, key, value)
            return call

        self.generic_visit(node)
        return node

    def visit_Slice(self, node):
        """
        Rewrite slice objects. Do this late in the pipeline so that other
        code can still recognize the code structure.
        """
        slice_values = [node.lower, node.upper, node.step]

        if self.nopython:
            raise error.NumbaError(node, "Cannot slice in nopython context")

        if all(isinstance(node, nodes.ConstNode) for node in slice_values):
            get_const = lambda node: None if node is None else node.pyval
            value = slice(get_const(node.lower), get_const(node.upper),
                          get_const(node.step))
            return self.visit(nodes.ObjectInjectNode(value))

        bounds = []
        for node in slice_values:
            if node is None:
                bounds.append(nodes.NULL_obj)
            else:
                bounds.append(node)

        new_slice = self.function_cache.call('PySlice_New', *bounds,
                                             temp_name='slice')
        return self.visit(new_slice)
        # return nodes.ObjectTempNode(new_slice)

    def visit_Attribute(self, node):
        if self.nopython:
            raise error.NumbaError(
                    node, "Cannot access Python attribute in nopython context")

        if node.type.is_numpy_attribute:
            return nodes.ObjectInjectNode(node.type.value)
        elif is_obj(node.value.type):
            node = self.function_cache.call(
                                'PyObject_GetAttrString', node.value,
                                nodes.ConstNode(node.attr))
            return self.visit(node)

        self.generic_visit(node)
        return node

    def visit_ExtTypeAttribute(self, node):
        ext_type = node.value.type
        offset = nodes.ConstNode(ext_type.attr_offset, Py_ssize_t)

        pointer = nodes.PointerFromObject(node.value)
        pointer = nodes.CoercionNode(pointer, char.pointer())
        pointer = nodes.pointer_add(pointer, offset)

        struct_pointer = nodes.CoercionNode(
                    pointer, ext_type.attribute_struct.pointer())
        if isinstance(node.ctx, ast.Load):
            struct_pointer = nodes.DereferenceNode(struct_pointer)
        attr = nodes.StructAttribute(struct_pointer, node.attr, node.ctx,
                                     ext_type.attribute_struct)
        attr.type = node.type
        return self.visit(attr)

    def visit_Name(self, node):
        if node.type.is_builtin and not node.variable.is_local:
            obj = getattr(builtins, node.name)
            return nodes.ObjectInjectNode(obj, node.type)

        return node

    def visit_Return(self, node):
        return_type = self.func_signature.return_type
        if node.value is not None:
            node.value = self.visit(nodes.CoercionNode(node.value, return_type))
        return node

    def visit_WithPythonNode(self, node):
        if not self.nopython:
            raise error.NumbaError(node, "Not in 'with nopython' context")

        self.nopython -= 1
        result = self.visitlist(node.body)
        self.nopython += 1

        return ast.Suite(body=result)

    def visit_WithNoPythonNode(self, node):
        if self.nopython:
            raise error.NumbaError(node, "Not in 'with python' context")

        self.nopython += 1
        result = self.visitlist(node.body)
        self.nopython -= 1

        return ast.Suite(body=result)

    def visit_For(self, node):
        self.generic_visit(node)
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        return node

