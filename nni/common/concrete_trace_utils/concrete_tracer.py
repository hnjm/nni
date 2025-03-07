# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import annotations

import sys
import inspect
import operator
import math
import functools
import builtins

from itertools import chain
from types import BuiltinMethodType, FunctionType, MethodDescriptorType, MethodType, MethodWrapperType, ModuleType
from typing import Any, Dict, Iterable, Iterator, Optional, Set, Tuple, Type, List, Callable, Union
from contextlib import contextmanager

import torch
from torch._C import ScriptObject
from torch.nn.modules.container import Sequential, ModuleList, ModuleDict, ParameterList, ParameterDict

from torch.fx import GraphModule
from torch.fx._compatibility import compatibility
from torch.fx._symbolic_trace import _Patcher, _proxyable_classes
from torch.fx.graph import Graph
from torch.fx.node import Target, Node
from torch.fx.proxy import TracerBase

from . import concrete_proxy as ep
from .operator_patcher import OperatorPatcherContext
from .utils import (
    _orig_module_call,
    _orig_module_getattr,
    _orig_module_getattribute,

    _orig_agfunc_apply,
    _orig_torch_assert,

    _orig_type,
    _orig_isinstance,
    _orig_getattr,

    _orig_range,
    _orig_int,
    _orig_bool,
    _orig_tuple,
    _orig_list,
    _orig_set,
    _orig_frozenset,
    _orig_dict,
    _orig_map,
    _orig_zip,
    _orig_enumerate,
    _orig_slice,

    _orig_len,
    _orig_not,
    _orig_is,
    _orig_is_not,
    _orig_contains,
    _orig_index,
)

HAS_VARSTUFF = inspect.CO_VARARGS | inspect.CO_VARKEYWORDS

@compatibility(is_backward_compatible=True)
class ConcreteTracer(TracerBase):
    """
    A model tracer similar to _symbolic_trace.Tracer, but with concrete execution and real value so we can pass complecate conditions
    and go into correct brunches.
    """

    default_autowrap_modules = (
        math,
    )
    default_autowrap_leaf_function: Dict[Any, Tuple[List[Tuple[Union[ModuleType, Type], str]], bool, Optional[Callable]]] = {
        # function
        _orig_len:                  ([], False, None),
        _orig_not:                  ([], False, None),
        _orig_is:                   ([], False, None),
        _orig_is_not:               ([], False, None),
        _orig_contains:             ([], False, None),
        _orig_index:                ([], False, None),

        # force-traced function
        torch.rand:                 ([], True, None),
        torch.randn:                ([], True, None),
        torch.randint:              ([], True, None),
        torch.rand_like:            ([], True, None),
        torch.randn_like:           ([], True, None),
        torch.randint_like:         ([], True, None),
        torch.randperm:             ([], True, None),

        # method
        Sequential.__getitem__:     ([], False, operator.getitem),
        Sequential.__len__:         ([], False, _orig_len),
        Sequential.__iter__:        ([], False, iter),

        ModuleList.__getitem__:     ([], False, operator.getitem),
        ModuleList.__len__:         ([], False, _orig_len),
        ModuleList.__iter__:        ([], False, iter),

        ModuleDict.__getitem__:     ([], False, operator.getitem),
        ModuleDict.__len__:         ([], False, _orig_len),
        ModuleDict.__iter__:        ([], False, iter),
        ModuleDict.__contains__:    ([], False, _orig_contains),

        ParameterList.__getitem__:  ([], False, operator.getitem),
        ParameterList.__len__:      ([], False, _orig_len),
        ParameterList.__iter__:     ([], False, iter),

        ParameterDict.__getitem__:  ([], False, operator.getitem),
        ParameterDict.__len__:      ([], False, _orig_len),
        ParameterDict.__iter__:     ([], False, iter),
        ParameterDict.__contains__: ([], False, _orig_contains),
    }
    # equals to `from torch.nn import functional as nn_functional`
    # to pass pyright check
    nn_functional = getattr(torch.nn, 'functional')
    # order: torch.nn.functional > torch._C._VariableFunctions > torch._C._nn > torch._C._TensorBase
    for name in torch.functional.__all__:
        attr = getattr(torch.functional, name)
        if attr not in default_autowrap_leaf_function:
            default_autowrap_leaf_function[attr] = ([], False, attr)
    for name in dir(nn_functional):
        attr = getattr(nn_functional, name)
        if callable(attr) and not _orig_isinstance(attr, Type) and not name.startswith('__')\
            and getattr(attr, '__module__', None) not in ('typing', 'torch.nn.modules.utils'):
            if attr not in default_autowrap_leaf_function:
                default_autowrap_leaf_function[attr] = ([], False, getattr(torch.functional, name, None))
            if hasattr(attr, '__module__') and attr.__module__ != 'torch.nn.functional':
                default_autowrap_leaf_function[attr][0].append((nn_functional, name))
    for name in dir(torch._C._VariableFunctions):
        attr = getattr(torch._C._VariableFunctions, name)
        if callable(attr) and not _orig_isinstance(attr, Type) and not name.startswith('__'):
            if attr not in default_autowrap_leaf_function:
                default_autowrap_leaf_function[attr] = ([], False, getattr(torch.functional, name, None))
    for name in dir(torch._C._nn):
        attr = getattr(torch._C._nn, name)
        if callable(attr) and not _orig_isinstance(attr, Type) and not name.startswith('__'):
            if attr not in default_autowrap_leaf_function:
                default_autowrap_leaf_function[attr] = ([], False, getattr(torch.functional, name, None))
            if hasattr(attr, '__module__') and attr.__module__ != 'torch._C._nn':
                default_autowrap_leaf_function[attr][0].append((torch._C._nn, name))
    for name in dir(torch._C._TensorBase):
        attr = getattr(torch._C._TensorBase, name)
        if callable(attr) and not _orig_isinstance(attr, Type) and not name.startswith('__'):
            if attr not in default_autowrap_leaf_function:
                to_func = getattr(torch.Tensor, name, None)
                to_func = None if to_func == attr else to_func
                default_autowrap_leaf_function[attr] = ([], False, to_func)

    default_autowrap_leaf_class: Dict[Type, Tuple[List[Tuple[Union[ModuleType, Type], str]], bool]] = {
        # class
        _orig_bool:                 ([], False),
        _orig_zip:                  ([], False),
        _orig_int:                  ([], False),

        # iterable class
        _orig_tuple:                ([], True),
        _orig_list:                 ([], True),
        _orig_set:                  ([], True),
        _orig_frozenset:            ([], True),
        _orig_dict:                 ([], True),
    }
    @compatibility(is_backward_compatible=True)
    def __init__(self, autowrap_modules: Tuple[ModuleType] = default_autowrap_modules,
                 autowrap_leaf_function = default_autowrap_leaf_function,
                 autowrap_leaf_class = default_autowrap_leaf_class,
                 leaf_module = (),
                 fake_middle_class = ()) -> None:
        """
        similar to _symbolic_trace.Tracer.__init__.
        remove the 'param_shapes_constant' because we can get real shape when executing.
        """
        super().__init__()

        # Functions we will eagerly wrap when we see them while tracing
        # this captures both `math.sqrt()` and `from math import sqrt` automatically
        self._autowrap_function_ids: Set[int] = {
            id(value) for name, value in chain(*[m.__dict__.items() for m in autowrap_modules])
            if not name.startswith("_") and callable(value)}

        # Python modules to apply autowrap to at the start, in addition to
        # modules we see while tracing
        self._autowrap_search: List[ModuleType] = list(autowrap_modules)

        self.submodule_paths: Optional[Dict[torch.nn.Module, str]] = None

        self.autowrap_leaf_function = autowrap_leaf_function
        self.autowrap_leaf_class = autowrap_leaf_class
        self.leaf_module = leaf_module
        self.fake_middle_class = fake_middle_class

    @contextmanager
    def do_temp_disable(self, call=False, attr=False, agfunc_apply=False):
        assert call | attr | agfunc_apply
        # to pass pyright check
        temp_disable_call, temp_disable_attr, temp_disable_agfunc_apply = False, False, False
        if call:
            self.temp_disable_call_level += 1
            temp_disable_call = self.temp_disable_call
            self.temp_disable_call = True
        if attr:
            self.temp_disable_attr_level += 1
            temp_disable_attr = self.temp_disable_attr
            self.temp_disable_attr = True
        if agfunc_apply:
            self.temp_disable_agfunc_apply_level += 1
            temp_disable_agfunc_apply = self.temp_disable_agfunc_apply
            self.temp_disable_agfunc_apply = True
        try:
            yield
        finally:
            if agfunc_apply:
                self.temp_disable_agfunc_apply = temp_disable_agfunc_apply
                self.temp_disable_agfunc_apply_level -= 1
            if attr:
                self.temp_disable_attr = temp_disable_attr
                self.temp_disable_attr_level -= 1
            if call:
                self.temp_disable_call = temp_disable_call
                self.temp_disable_call_level -= 1

    @compatibility(is_backward_compatible=True)
    def fetch_attr(self, target: str) -> Any:
        """
        to get the attr in self.root. only for execution of 'call_module' nodes.
        """
        with self.do_temp_disable(attr=True):
            target_atoms = target.split('.')
            attr_itr = self.root
            for i, atom in _orig_enumerate(target_atoms):
                # if atom == '':
                #     continue
                if not hasattr(attr_itr, atom):
                    raise RuntimeError(f"Node referenced nonexistent target \'{'.'.join(target_atoms[:i])}\'")
                attr_itr = _orig_getattr(attr_itr, atom)
            return attr_itr

    def run_target(self, kind: str, target: Target, args: Tuple[Any, ...], kwargs: Dict[str, Any]):
        """
        actually execute the code.
        apply the patcher, and the _autowrap_check to the target function.
        """
        if kind == 'call_function':
            assert isinstance(target, Callable)
            fn = target
            if _orig_getattr(fn, '__module__', None) != 'nni.common.concrete_trace_utils.concrete_tracer' and hasattr(fn, '__globals__'):
                _autowrap_check(self, fn.__globals__, self._autowrap_function_ids, self.autowrap_leaf_pairs, self.agfunc_dict)
            with self.do_temp_disable(call=True):
                return OperatorPatcherContext.patch_run(fn, *args, **kwargs)
        elif kind == 'call_method':
            self_obj, *args_tail = args
            fn = _orig_getattr(self_obj, target)
            if _orig_getattr(fn, '__module__', None) != 'nni.common.concrete_trace_utils.concrete_tracer' and hasattr(fn, '__globals__'):
                _autowrap_check(self, fn.__globals__, self._autowrap_function_ids, self.autowrap_leaf_pairs, self.agfunc_dict)
            with self.do_temp_disable(call=True):
                return OperatorPatcherContext.patch_run(fn, *args_tail, **kwargs)
        elif kind == 'call_module':
            assert isinstance(target, str)
            mod = self.fetch_attr(target)
            if _orig_getattr(mod, '__module__', None) != 'nni.common.concrete_trace_utils.concrete_tracer' and hasattr(mod, '__globals__'):
                _autowrap_check(self, mod.__globals__, self._autowrap_function_ids, self.autowrap_leaf_pairs, self.agfunc_dict)
            with self.do_temp_disable(call=True):
                return OperatorPatcherContext.patch_run(mod, *args, **kwargs)
        elif kind == 'get_attr':
            assert isinstance(target, str)
            return self.fetch_attr(target)
        elif kind == 'output':
            return args[0]
        elif kind == 'placeholder':
            return self.placeholder_dict[target]
        else:
            raise RuntimeError()

    @compatibility(is_backward_compatible=True)
    def proxy(self, value: Any, node: Node) -> ep.ConcreteProxy:
        """
        overloaded to use custom 'proxy'.
        """
        return ep.ConcreteProxy(node, value, self)

    @compatibility(is_backward_compatible=True)
    def create_proxy(self, kind: str, target: Target, args: Tuple[Any, ...], kwargs: Dict[str, Any],
                     name: Optional[str] = None, type_expr: Optional[Any] = None):
        """
        similar to _symbolic_trace.Tracer.create_proxy.
        use the 'run_target' to actually execute the code, and store the value in 'value' field.
        """
        def upwrapper(obj: Any):
            while _orig_isinstance(obj, ep.ConcreteProxy):
                obj = obj.value
            return obj
        args_unwrapped = ep.map_aggregate_not_proxy(args, upwrapper)
        kwargs_unwrapped = ep.map_aggregate_not_proxy(kwargs, upwrapper)

        # real value by execution
        value_unwrapped = self.run_target(kind, target, args_unwrapped, kwargs_unwrapped)

        args_noded = self.create_arg(args)
        kwargs_noded = self.create_arg(kwargs)

        assert isinstance(args_noded, tuple)
        assert isinstance(kwargs_noded, dict)

        node = self.create_node(kind, target, args_noded, kwargs_noded, name, type_expr)
        return self.proxy(value_unwrapped, node)

    @compatibility(is_backward_compatible=True)
    def create_arg(self, a: Any) -> Union[Node, Any]:
        """
        similar to _symbolic_trace.Tracer.create_arg
        move the base case to the top in case the wrapping of the function 'isinstance'
        """
        # base case: we unwrap the Proxy object
        if isinstance(a, ep.ConcreteProxy):
            return a.node

        if isinstance(a, torch.nn.Parameter):
            for n, p in self.root.named_parameters():
                if a is p:
                    return self.create_node('get_attr', n, (), {})
            raise NameError('parameter is not a member of this module')
        elif isinstance(a, torch.Tensor):
            for n_, p_ in self.root.named_buffers():
                if a is p_:
                    return self.create_node('get_attr', n_, (), {})
        elif isinstance(a, torch.nn.Module):
            for n_, p_ in self.root.named_modules():
                if a is p_:
                    return self.create_node('get_attr', n_, (), {})
        # for slice
        if isinstance(a, slice):
            start = self.create_arg(a.start)
            stop = self.create_arg(a.stop)
            step = self.create_arg(a.step)
            if _orig_isinstance(start, Node)\
                or _orig_isinstance(stop, Node)\
                or _orig_isinstance(step, Node):
                return self.create_node('call_function', _orig_slice, (start, stop, step), {})
            else:
                return a
        # For NamedTuple instances that appear literally as args, we emit
        # a node to construct the NamedTuple and use that Node as the argument.
        if isinstance(a, tuple) and hasattr(a, '_fields'):
            args = tuple(self.create_arg(elem) for elem in a)
            return self.create_node('call_function', a.__class__, args, {})

        # Tensors do not have a reliable string repr() from which they can be
        # constructed (and we probably don't want to rely on that, either), so
        # for any constant Tensor values we encounter, first search for if they
        # are an attribute of some module in the module hierarchy. If so, emit
        # a get_attr to retrieve that tensor. Otherwise, we'll store away the
        # tensor value into a special attribute on the Module s.t. we can
        # retrieve it with a get_attr.
        if isinstance(a, (torch.Tensor, ScriptObject)):
            qualname: Optional[str] = self.tensor_attrs.get(a)

            # Tensor was not found in the Module hierarchy, stow it away in a
            # TODO: warning for the not found tensor
            if not qualname:
                i = 0
                while True:
                    qualname = f'_tensor_constant{i}'
                    if not hasattr(self.root, qualname):
                        break
                    i += 1
                self.tensor_attrs[a] = qualname
                setattr(self.root, qualname, a)

            return self.create_node('get_attr', qualname, (), {})

        if _orig_type(a) in _proxyable_classes:
            # This is an instance of a proxyable class for which we did not
            # witness its construction. Intern this as a constant attribute

            # TODO: binary search
            i = 0
            while True:
                qualname = f'_{a.__class__.__name__}_constant_{i}'
                if not hasattr(self.root, qualname):
                    break
                i += 1
            setattr(self.root, qualname, a)

            return self.create_node('get_attr', qualname, (), {})

        if isinstance(a, (torch.autograd.function.Function, torch.autograd.function.FunctionMeta)):
            return a

        return super().create_arg(a)

    @compatibility(is_backward_compatible=True)
    def is_leaf_module(self, m: torch.nn.Module, module_qualified_name: str) -> bool:
        """
        similar to _symbolic_trace.Tracer.is_leaf_module
        """
        return (m.__module__.startswith('torch.nn') and not _orig_isinstance(m, (Sequential, ModuleList, ModuleDict)))\
            or _orig_isinstance(m, self.leaf_module)

    @compatibility(is_backward_compatible=True)
    def path_of_module(self, mod: torch.nn.Module) -> str:
        """
        similar to _symbolic_trace.Tracer.path_of_module
        """
        # Prefer the O(1) algorithm
        if self.submodule_paths:
            path = self.submodule_paths.get(mod)
            # TODO: better infomation
            if path is None:
                if not hasattr(self.root, '_module_constants'):
                    self.root._module_constants = torch.nn.ModuleList()
                module_constants = self.root._module_constants
                assert isinstance(module_constants, torch.nn.ModuleList)
                if hasattr(mod, 'extra_repr'):
                    sub_path = _orig_type(mod).__name__ + mod.extra_repr()
                else:
                    sub_path = str(_orig_len(module_constants))
                if not hasattr(module_constants, sub_path):
                    module_constants.add_module(sub_path, mod)
                path = '_module_constants.%s' % sub_path
                self.submodule_paths[mod] = path
                return path
            assert isinstance(path, str)
            return path
        # O(N^2) fallback in the case that we didn't store the submodule
        # paths.
        else:
            for n, p in self.root.named_modules():
                if mod is p:
                    return n
            raise NameError('module is not installed as a submodule')

    # This method will be refactored
    @compatibility(is_backward_compatible=False)
    def create_args_for_root(self, root_fn, is_module, concrete_args: Union[Dict[str, Any], Tuple]) -> Tuple[Any, list, Any, Any]:
        """
        for wrapping all the parameters of the function with dummy_input.
        in concrete tracer, we need all the parameters input by users.

        todo: this function should be refactored after the same function in torch.fx be refactored.
        """
        # In some cases, a function or method has been decorated with a wrapper
        # defined via ``functools.wraps``. In this case, the outer code object
        # will likely not contain the actual parameters we care about, so unwrap
        # the function to get to the innermost callable.
        fn_for_analysis = inspect.unwrap(root_fn)
        default_value_list = fn_for_analysis.__defaults__
        if default_value_list is None:
            default_value_list = tuple()
        co = fn_for_analysis.__code__
        total_args = co.co_argcount + co.co_kwonlyargcount
        # orig_args = list(co.co_varnames)
        names_iter = iter(co.co_varnames)
        args: List[Any] = []
        more_args = []
        kwargs = {}
        skip_arg_idx = 0
        if is_module:
            if total_args == 0:
                raise RuntimeError('``self`` argument cannot be part of *args expansion!')
            skip_arg_idx = 1
            next(names_iter)  # skip self
            args.append(self.root)

        cnt = 0
        self.placeholder_dict = {}
        arg_names = [next(names_iter) for idx in range(skip_arg_idx, total_args)]
        diff_len = len(arg_names) - len(default_value_list)
        default_args = {arg_names[idx + diff_len]: default_value_list[idx] for idx in range(len(default_value_list))}
        if isinstance(concrete_args, tuple):
            if len(arg_names) != len(concrete_args):
                raise RuntimeError(f"Tracing expected {len(arg_names)} arguments but got {len(concrete_args)} concrete arguments")
            concrete_args = {name: val for name, val in zip(arg_names, concrete_args)}
        def proxy_placeholder(name: str):
            nonlocal cnt
            cnt += 1

            default_arg = ()
            if name in default_args and not name.startswith('*'):
                default_arg = (default_args[name],)

            if name in concrete_args:
                self.placeholder_dict[name] = concrete_args[name]
            else:
                # TODO: better infomation
                assert name in default_args
                self.placeholder_dict[name] = default_args[name]
            return self.create_proxy('placeholder', name, default_arg, {})
        args.extend(proxy_placeholder(names) for names in arg_names)


        if hasattr(co, 'co_kwonlyargcount') and (
            co.co_kwonlyargcount > 0 or co.co_flags & HAS_VARSTUFF):
            # TODO: type annotations for *args and **kwargs
            if co.co_flags & inspect.CO_VARARGS:
                name = '*' + next(names_iter)
                default_args[name] = ()
                more_args = proxy_placeholder(name)
            if co.co_flags & inspect.CO_VARKEYWORDS:
                name = '**' + next(names_iter)
                default_args[name] = {}
                kwargs = proxy_placeholder(name)

        return root_fn, args, more_args, kwargs

    @compatibility(is_backward_compatible=True)
    def trace(self, root: Union[torch.nn.Module, Callable[..., Any]],
                concrete_args: Union[Dict[str, Any], Tuple],
                use_operator_patch: bool = True,
                operator_patch_backlist: List[str] = [],
                forwrad_function_name: str = 'forward') -> Graph:
        """
        similar to _symbolic_trace.Tracer.trace
        different args:
            use_operator_patch:
                the operators 'not/is/is not/in/not in' cannot be wrapped after
                    compiled. so we re-parse the functions, replace these operators
                    with functions 'operator.not_/is_/is_not/contains', then we
                    could wrap and trace these.
                for example: in ``if x is None:``, if x is a proxy, the tracer will
                    never go into the branch, even x is a proxy with value 'None'.
                values:
                true: before executing a func, the func will be patched if the func
                    is not in operator_patch_backlist
                false: before executing a func, the func will be patched if the func
                    is in operator_patch_backlist

            operator_patch_backlist:
                such as '__main__.FooModel' or '__main__.bar_func'. the namespace is
                always needed.
        """
        if isinstance(root, torch.nn.Module):
            self.root = root

            # TODO: better infomation
            assert hasattr(
                root, forwrad_function_name
            ), f"traced_func_name={forwrad_function_name} doesn't exist in {_orig_type(root).__name__}"

            fn = getattr(root, forwrad_function_name)
            self.submodule_paths = {mod: name for name, mod in root.named_modules()}
        else:
            self.root = torch.nn.Module()
            fn = root

        tracer_cls = getattr(self, '__class__', None)
        self.graph = Graph(tracer_cls=tracer_cls)

        # When we encounter a Tensor value that's not a parameter, we look if it
        # is some other attribute on the model. Construct a dict mapping Tensor
        # values to the qualified name here for efficiency. This is used downstream
        # in create_arg
        self.tensor_attrs: Dict[Union[torch.Tensor, ScriptObject], str] = {}

        def collect_tensor_attrs(m: torch.nn.Module, prefix_atoms: List[str]):
            for k, v in m.__dict__.items():
                if isinstance(v, (torch.Tensor, ScriptObject)):
                    self.tensor_attrs[v] = '.'.join(prefix_atoms + [k])
            for k, v in m.named_children():
                collect_tensor_attrs(v, prefix_atoms + [k])

        collect_tensor_attrs(self.root, [])

        if isinstance(fn, MethodType):
            fn = fn.__func__
        assert isinstance(fn, FunctionType)

        fn_globals = fn.__globals__  # run before it gets patched
        fn, args, more_args, kwargs = self.create_args_for_root(fn, isinstance(root, torch.nn.Module), concrete_args)

        self.the_path_of_parameter = {id(v): k for k, v in self.root.named_parameters()}
        self.the_path_of_buffer = {id(v): k for k, v in self.root.named_buffers()}

        def get_middle_class(node, memo = set(), prefix = ''):
            if node not in memo:
                memo.add(node)
                yield prefix, node
                if isinstance(node, torch.nn.Module):
                    items = (*((k, v) for k, v in node.__dict__.items() if not k.startswith('_')), *node._modules.items())
                else:
                    items = ((k, v) for k, v in node.__dict__.items() if not k.startswith('_'))
                for name, subfield in items:
                    if isinstance(subfield, (torch.nn.Module, self.fake_middle_class)):
                        submodule_prefix = prefix + ('.' if prefix else '') + name
                        for m in get_middle_class(subfield, memo, submodule_prefix):
                            yield m
        self.the_path_of_middle_class = {id(v): k for k, v in get_middle_class(self.root)}

        @functools.wraps(_orig_module_getattribute)
        def module_getattribute_wrapper(mod, attr):
            if self.temp_disable_call | self.temp_disable_attr:
                try:
                    return _orig_module_getattribute(mod, attr)
                except AttributeError:
                    return _orig_module_getattr(mod, attr)
            with self.do_temp_disable(call=True, attr=True):
                try:
                    attr_val = _orig_module_getattribute(mod, attr)
                except AttributeError:
                    attr_val = _orig_module_getattr(mod, attr)
            if callable(attr_val):
                if attr_val in self.wrapped_leaf:
                    return self.wrapped_leaf[attr_val][1]
                return attr_val
            elif _orig_isinstance(attr_val, (_orig_tuple, _orig_list)):
                if self.the_path_of_middle_class[id(mod)] == '':
                    return self.create_proxy('get_attr', f'{attr}', (), {})
                else:
                    return self.create_proxy('get_attr', f'{self.the_path_of_middle_class[id(mod)]}.{attr}', (), {})
            elif id(attr_val) in self.the_path_of_parameter:
                return self.create_proxy('get_attr', self.the_path_of_parameter[id(attr_val)], (), {})
            elif id(attr_val) in self.the_path_of_buffer:
                return self.create_proxy('get_attr', self.the_path_of_buffer[id(attr_val)], (), {})
            return attr_val

        @functools.wraps(_orig_module_call)
        def module_call_wrapper(mod, *args, **kwargs):
            if self.temp_disable_call:
                return _orig_module_call(mod, *args, **kwargs)
            else:
                module_qualified_name = self.path_of_module(mod)
                if not self.is_leaf_module(mod, module_qualified_name):
                    _autowrap_check(self, mod.forward.__globals__, self._autowrap_function_ids, self.autowrap_leaf_pairs, self.agfunc_dict)
                    _autowrap_check(self, mod.__dict__, self._autowrap_function_ids, self.autowrap_leaf_pairs, self.agfunc_dict)
                    return _orig_module_call(mod, *args, **kwargs)
                else:
                    return self.create_proxy('call_module', module_qualified_name, args, kwargs)

        class map_wrapper_clz:
            @functools.wraps(_orig_map)
            def __call__(self, the_func, *iterables: Any):
                tracers = _orig_set()
                for one_iter in iterables:
                    if _orig_isinstance(one_iter, ep.Proxy):
                        tracers.add(one_iter.tracer)
                if _orig_len(tracers) > 1:
                    raise Exception('more than 1 tracer detected. please report the issue')
                elif _orig_len(tracers) == 1:
                    results = _orig_list()
                    for args in _orig_zip(*iterables):
                        results.append(the_func(*args))
                    return next(iter(tracers)).create_proxy('call_function', _orig_tuple, (results,), {})

                ## for the multi-level list/tuple
                iterables = _orig_list(_orig_list(it) for it in iterables)
                for it in iterables:
                    for arg in it:
                        if _orig_isinstance(arg, ep.Proxy):
                            tracers.add(arg.tracer)
                if _orig_len(tracers) > 1:
                    raise Exception('more than 1 tracer detected. please report the issue')
                elif _orig_len(tracers) == 1:
                    results = _orig_list()
                    for args in _orig_zip(*iterables):
                        results.append(the_func(*args))
                    return next(iter(tracers)).create_proxy('call_function', _orig_tuple, (results,), {})
                ## for the multi-level list/tuple end

                return _orig_map(the_func, *iterables)
            def __eq__(self, __o: object) -> bool:
                return id(__o) in (id(self), id(_orig_map))
            def __hash__(self):
                return id(self)
        map_wrapper = map_wrapper_clz()

        class range_wrapper_clz:
            @functools.wraps(_orig_range)
            def __call__(self, *args):
                # TODO: better infomation
                assert 1 <= _orig_len(args) <= 3
                args = (arg.value if _orig_isinstance(arg, ep.ConcreteProxy) else arg for arg in args)
                return _orig_range(*args)
            def __eq__(self, __o: object) -> bool:
                return id(__o) in (id(self), id(_orig_range))
            def __hash__(self):
                return id(self)
        range_wrapper = range_wrapper_clz()

        class enumerate_wrapper_clz:
            @functools.wraps(_orig_enumerate)
            def __call__(self, iterable, start=0):
                count = start
                for elem in iterable:
                    if _orig_isinstance(elem, ep.ConcreteProxy) and _orig_isinstance(elem.value, (_orig_int, str)):
                        yield count, elem.value
                    else:
                        yield count, elem
                    count += 1
            def __eq__(self, __o: object) -> bool:
                return id(__o) in (id(self), id(_orig_enumerate))
            def __hash__(self):
                return id(self)
        enumerate_wrapper = enumerate_wrapper_clz()

        class type_wrapper_clz:
            @functools.wraps(_orig_type)
            def __call__(self, instance):
                orig_type = _orig_type(instance)
                if orig_type in (ep.ConcreteProxy, ep.ConcreteAttrProxy, ep.ConcreteUnpackIterProxy):
                    return _orig_type(instance.value)
                else:
                    return orig_type
            def __eq__(self, __o: object) -> bool:
                return id(__o) in (id(self), id(_orig_enumerate))
            def __hash__(self):
                return id(self)
        type_wrapper = type_wrapper_clz()

        @classmethod
        @functools.wraps(_orig_agfunc_apply)
        def agfunc_apply_wrapper(clz, *args, **kwargs):
            if clz not in self.agfunc_dict:
                self.agfunc_dict[clz] = torch._C._FunctionBase.__dict__['apply'].__get__(None, clz)
            if self.temp_disable_agfunc_apply or self.temp_disable_call:
                return self.agfunc_dict[clz](*args, **kwargs)
            tracers = _orig_set()
            def unwrap_detect_tracers(obj):
                if isinstance(obj, ep.ConcreteProxy):
                    tracers.add(obj.tracer)
            ep.map_aggregate_not_proxy(args, unwrap_detect_tracers)
            ep.map_aggregate_not_proxy(kwargs, unwrap_detect_tracers)
            if _orig_len(tracers) == 0:
                return self.agfunc_dict[clz](*args, **kwargs)
            elif _orig_len(tracers) == 1 and next(iter(tracers)) == self:
                return self.create_proxy('call_function', self.agfunc_dict[clz], args, kwargs)
            else:
                raise Exception('more than 1 tracer detected. please report the issue')

        @functools.wraps(_orig_torch_assert)
        def torch_assert_wrapper(condition, message):
            while _orig_isinstance(condition, ep.ConcreteProxy):
                condition = condition.value
            return _orig_torch_assert(condition, message)

        self.agfunc_dict: dict[Type, Any] = {}
        self.autowrap_leaf_pairs = {
            id(_orig_torch_assert): torch_assert_wrapper,
        }
        self.wrapped_leaf = dict()

        for func, (positions, is_force_trace, to_func) in self.autowrap_leaf_function.items():
            if _orig_isinstance(func, BuiltinMethodType) and getattr(func, '__name__', None) == 'apply'\
                and _orig_isinstance(getattr(func, '__self__', None), Type) and issubclass(func.__self__, torch.autograd.Function):
                # torch.autograd.function
                assert to_func == None, '<subclass of torch.autograd.Function>.apply should set to_func to None!'
                if func.__self__ not in self.agfunc_dict:
                    self.agfunc_dict[func.__self__] = _create_wrapped_leaf_func(self, func, func)
                wrapped = self.agfunc_dict[func.__self__]
            else:
                if func.__qualname__.startswith('_TensorBase'):
                    positions = (*positions, (torch.Tensor, func.__name__))
                    wrapped = _create_wrapped_leaf_method(self, getattr(torch.Tensor, func.__name__), func.__name__, to_func)
                elif func.__qualname__.startswith('_VariableFunctionsClass'):
                    if hasattr(torch, func.__name__):
                        # avoid bad attr like 'unique_dim'
                        positions = (*positions, (torch, func.__name__))
                    if is_force_trace:
                        wrapped = _create_wrapped_leaf_func(self, func, to_func, (self,))
                    else:
                        wrapped = _create_wrapped_leaf_func(self, func, to_func)
                elif _orig_isinstance(func, (MethodDescriptorType, MethodWrapperType)):
                    wrapped = _create_wrapped_leaf_method(self, func, func.__name__, to_func)
                elif func.__name__ != func.__qualname__ and func.__qualname__ != 'boolean_dispatch.<locals>.fn':
                    # method
                    if func.__module__.startswith('_') and func.__module__ != '__main__':
                        path = sys.modules[func.__module__[1:]]
                    else:
                        path = sys.modules[func.__module__]
                    path = getattr(path, func.__qualname__.split('.')[0])
                    positions = (*positions, (path, func.__name__))
                    wrapped = _create_wrapped_leaf_method(self, func, func.__name__, to_func)
                else:
                    # common function
                    if func.__module__.startswith('_') and func.__module__ != '__main__':
                        path = sys.modules[func.__module__[1:]]
                    else:
                        path = sys.modules[func.__module__]
                    positions = (*positions, (path, func.__name__))
                    if is_force_trace:
                        wrapped = _create_wrapped_leaf_func(self, func, to_func, (self,))
                    else:
                        wrapped = _create_wrapped_leaf_func(self, func, to_func)
            self.wrapped_leaf[func] = (positions, wrapped)

        self.clz_wrapper_map: Dict[Any, Type] = {
            map_wrapper: _orig_map,
            enumerate_wrapper: _orig_enumerate,
            range_wrapper: _orig_range,
            type_wrapper: _orig_type,
        }
        for clz, (positions, is_iterable) in self.autowrap_leaf_class.items():
            if clz.__module__.startswith('_') and clz.__module__ != '__main__':
                path = sys.modules[clz.__module__[1:]]
            else:
                path = sys.modules[clz.__module__]
            if is_iterable:
                wrapped = _create_wrapped_leaf_iterable_class(self, clz)
            else:
                wrapped = _create_wrapped_leaf_class(self, clz)
            positions = (*positions, (path, clz.__name__))
            self.wrapped_leaf[clz] = (positions, wrapped)
            self.clz_wrapper_map[wrapped] = clz

        for clz in self.fake_middle_class:
            wrapped = _create_wrapped_attr_for_middle_class(self, clz, self.the_path_of_middle_class)
            self.wrapped_leaf[clz.__getattribute__] = (((clz, '__getattribute__'),), wrapped)

        @functools.wraps(_orig_isinstance)
        def isinstance_wrapper(instance, clz):
            if _orig_type(clz) in (slice, tuple, list, _orig_slice, _orig_tuple, _orig_list):
                clz_wrapped = []
                for wrapped_type, orig_type in self.clz_wrapper_map.items():
                    if wrapped_type in clz:
                        clz_wrapped.append(orig_type)
                clz = (*clz_wrapped, *(aclz for aclz in clz if aclz not in self.clz_wrapper_map))
                # use _orig_isinstance(clz, Iterable) will cause an endless recursive loop
                for cls in (object, ep.ConcreteProxy, ep.ConcreteAttrProxy, ep.ConcreteUnpackIterProxy):
                    if cls in clz and _orig_isinstance(instance, cls):
                        return True
                if _orig_isinstance(instance, ep.ConcreteProxy):
                    return _orig_isinstance(instance.value, clz)
                else:
                    return _orig_isinstance(instance, clz)
            else:
                if clz in (object, ep.ConcreteProxy, ep.ConcreteAttrProxy, ep.ConcreteUnpackIterProxy):
                    return _orig_isinstance(instance, clz)
                if clz in self.clz_wrapper_map:
                    clz = self.clz_wrapper_map[clz]
                if _orig_isinstance(instance, ep.ConcreteProxy):
                    instance = instance.value
                return _orig_isinstance(instance, clz)

        @functools.wraps(_orig_getattr)
        def getattr_wrapper(obj, *args):
            # TODO: better infomation
            if not 1 <= _orig_len(args) <= 2:
                raise Exception()
            args = _orig_list(args)
            if _orig_isinstance(args[0], ep.ConcreteProxy):
                args[0] = args[0].value
            return _orig_getattr(obj, *args)

        # for passing the tracing of leaf modules
        self.temp_disable_call = False
        self.temp_disable_attr = False
        self.temp_disable_agfunc_apply = False
        self.temp_disable_call_level = 0
        self.temp_disable_attr_level = 0
        self.temp_disable_agfunc_apply_level = 0
        try:
            with _Patcher() as self.patcher:
                # allow duplicate patches to support the case of nested calls
                self.patcher.patch_method(torch.nn.Module, "__getattribute__", module_getattribute_wrapper, deduplicate=False)

                self.patcher.patch_method(torch.nn.Module, "__call__", module_call_wrapper, deduplicate=False)
                self.patcher.patch_method(torch.autograd.Function, "apply", agfunc_apply_wrapper, deduplicate=False)
                self.patcher.patch_method(torch, "_assert", torch_assert_wrapper, deduplicate=False)

                self.patcher.patch_method(builtins, "map", map_wrapper, deduplicate=False)
                self.patcher.patch_method(builtins, "enumerate", enumerate_wrapper, deduplicate=False)
                self.patcher.patch_method(builtins, "range", range_wrapper, deduplicate=False)
                self.patcher.patch_method(builtins, "type", type_wrapper, deduplicate=False)
                self.patcher.patch_method(builtins, "isinstance", isinstance_wrapper, deduplicate=False)
                self.patcher.patch_method(builtins, "getattr", getattr_wrapper, deduplicate=False)

                for obj, (positions, wrapped) in self.wrapped_leaf.items():
                    for path, name in positions:
                        self.patcher.patch_method(path, name, wrapped, deduplicate=False)
                    self.autowrap_leaf_pairs[id(obj)] = wrapped

                _patch_wrapped_functions(self.patcher)
                _autowrap_check(self, fn_globals, self._autowrap_function_ids, self.autowrap_leaf_pairs, self.agfunc_dict)
                for module in self._autowrap_search:
                    _autowrap_check(self, module.__dict__, self._autowrap_function_ids, self.autowrap_leaf_pairs, self.agfunc_dict)
                with OperatorPatcherContext(self, use_operator_patch, operator_patch_backlist):
                    self.create_node('output', 'output',
                                    (self.create_arg(OperatorPatcherContext.patch_run(fn, *args, *more_args, **kwargs)),),
                                    {}, type_expr=fn.__annotations__.get('return', None))
        finally:
            # for cuda versions of pytorch, autograd.Function.apply should be reverted manually
            delattr(torch.autograd.Function, 'apply')
            pass

        self.submodule_paths = None
        return self.graph

# List of pairs of (global dict, function name) functions
# to patch for the purposes of the wrap() API.
_wrapped_fns_to_patch : List[Tuple[dict, str]] = []

# List of methods on classes to wrap (class type, function name)
# this currently only works for Tensor.* methods that aren't traced properly
_wrapped_methods_to_patch : List[Tuple[type, str]] = []


def _find_proxy(*objects_to_search):
    """
    Recursively search a data structure for a Proxy() and return it,
    return None if not found.
    """
    proxy = None

    def find_proxy(x):
        nonlocal proxy
        if isinstance(x, ep.ConcreteProxy):
            proxy = x

    ep.map_aggregate_not_proxy(objects_to_search, find_proxy)
    return proxy

def _create_wrapped_func(orig_fn):
    @functools.wraps(orig_fn)
    def wrapped(*args, **kwargs):
        """
        Given an closed-over ``orig_function`` to invoke, search the args and kwargs for
        a Proxy object. If there is one, emit a ``call_function`` node to preserve the
        call to this leaf function directly. Otherwise, just return the results of
        this function call, as this function is not being traced.
        """
        proxy = _find_proxy(args, kwargs)
        if proxy is not None:
            return_proxy = proxy.tracer.create_proxy('call_function', orig_fn, args, kwargs)
            return_proxy.node.meta['is_wrapped'] = True
            return return_proxy
        return orig_fn(*args, **kwargs)

    return wrapped

def _patch_wrapped_functions(patcher : _Patcher):
    """
    Go through ``_wrapped_fn_patch_table`` and, for each frame object, wrap
    the listed global functions in the `_create_wrapped_func` wrapper.
    """
    for frame_dict, name in _wrapped_fns_to_patch:
        if name not in frame_dict and hasattr(builtins, name):
            orig_fn = _orig_getattr(builtins, name)
        else:
            orig_fn = frame_dict[name]
        patcher.patch(frame_dict, name, _create_wrapped_func(orig_fn))

    for cls, name in _wrapped_methods_to_patch:
        patcher.patch_method(cls, name, _create_wrapped_method(cls, name))

def _autowrap_check(tracer: ConcreteTracer, frame_dict : Dict[str, Any], function_ids : Set[int],\
    function_pairs : Dict[int, Callable], agfunc_dict: dict[Type, Any]):
    """
    Some methods, like `math.sqrt` are common enough we want to automatically wrap them as we see them.
    This method searches a scope for them and patches them if found.
    """
    patcher = tracer.patcher
    if patcher.visit_once(frame_dict):
        for name, value in frame_dict.items():
            # if callable(value) and (not name.startswith('_') or name == '_assert'):
            if callable(value) and not name.startswith('__') and not name.startswith('_orig_'):
                if id(value) in function_ids:
                    patcher.patch(frame_dict, name, _create_wrapped_func(value))
                elif id(value) in function_pairs:
                    patcher.patch(frame_dict, name, function_pairs[id(value)])
                elif _orig_isinstance(value, BuiltinMethodType) and getattr(value, '__name__', None) == 'apply'\
                    and _orig_isinstance(getattr(value, '__self__', None), Type) and issubclass(value.__self__, torch.autograd.Function):
                    # torch.autograd.function
                    if value.__self__ not in agfunc_dict:
                        agfunc_dict[value.__self__] = _create_wrapped_leaf_func(tracer, value, value)
                    patcher.patch(frame_dict, name, agfunc_dict[value.__self__])

def _create_wrapped_method(cls, name):
    orig_fn = _orig_getattr(cls, name)

    @functools.wraps(orig_fn)
    def wrapped(*args, **kwargs):
        """
        Search the args and kwargs for a Proxy object. If there is one,
        emit a ``call_method`` node to preserve the call to this method
        directly. Otherwise, just return the results of this function
        call, as this function is not being traced.
        """
        proxy = _find_proxy(args, kwargs)
        if proxy is not None:
            return proxy.tracer.create_proxy('call_method', name, args, kwargs)
        return orig_fn(*args, **kwargs)

    return wrapped


@compatibility(is_backward_compatible=True)
class GraphAppendingConcreteTracer(ConcreteTracer):
    def __init__(self, graph: Graph):
        super().__init__()
        self.graph = graph

class MagicMethodPatcher:
    from torch.fx import graph as fx_graph
    from torch.fx import graph_module as fx_graph_module
    from torch.fx import node as fx_node
    magic_methods_ori = fx_graph.magic_methods
    magic_methods_new = {
        **fx_graph.magic_methods,
        'not_': 'not {}',
        'is_': '{} is {}',
        'is_not': '{} is not {}',
        'contains': '{1} in {0}',
    }
    format_target_ori: Any = fx_graph._format_target
    copy_attr_ori: Any = fx_graph_module._copy_attr
    find_module_of_method_ori: Any = fx_node._find_module_of_method

    @staticmethod
    def copy_attr_new(from_module: torch.nn.Module, to_module: torch.nn.Module, target: str):
        *prefix, field = target.split('.')
        for item in prefix:
            f = getattr(from_module, item)
            t = getattr(to_module, item, None)
            if f is t:
                return

            if t is None:
                if isinstance(f, Sequential):
                    t = Sequential()
                elif isinstance(f, ModuleList):
                    t = ModuleList()
                elif isinstance(f, ModuleDict):
                    t = ModuleDict()
                else:
                    t = torch.nn.Module()
                if hasattr(f, '_get_name'):
                    t._get_name = f._get_name
                to_module.add_module(item, t)
            from_module, to_module = f, t

        orig = getattr(from_module, field)
        # If it is a tensor and not a parameter attribute of a module, it should be a named buffer.
        # So, we register it as a named buffer in the target module.
        if isinstance(orig, torch.Tensor) and not isinstance(orig, torch.nn.Parameter):
            to_module.register_buffer(field, orig)
        else:
            setattr(to_module, field, orig)

    def format_target_new(self, base: str, target: str) -> str:
        elems = target.split('.')
        r = base
        field = None
        if r == 'self':
            field = self.root
        for e in elems:
            if e.isidentifier():
                r = f'{r}.{e}'
            elif isinstance(field, (Sequential, ModuleList, ParameterList, ModuleDict, ParameterDict)):
                r = f'{r}.get_submodule(\'{e}\')'
            else:
                r = f'getattr({r}, "{e}")'
            if field is not None:
                field = getattr(field, e)
        return r

    @staticmethod
    def find_module_of_method_new(orig_method: Callable[..., Any]) -> str:
        name = orig_method.__name__
        module = orig_method.__module__
        if module is not None:
            return module
        elif hasattr(orig_method, '__qualname__')\
            and isinstance(orig_method.__qualname__, str) and orig_method.__qualname__.startswith('_VariableFunctionsClass.'):
            return 'torch._C._VariableFunctions'
        elif hasattr(orig_method, '__self__')\
            and isinstance(orig_method.__self__, Type) and issubclass(orig_method.__self__, torch.autograd.Function):
            # for torch.autograd.Function
            return f'{orig_method.__self__.__module__}.{orig_method.__self__.__name__}'
        for guess in [torch, getattr(torch.nn, 'functional')]:
            if getattr(guess, name, None) is orig_method:
                return guess.__name__
        raise RuntimeError(f'cannot find module for {orig_method}')

    def __init__(self, root):
        self.root = root

    def __enter__(self):
        MagicMethodPatcher.fx_graph.magic_methods = self.magic_methods_new
        MagicMethodPatcher.fx_graph._format_target = self.format_target_new
        MagicMethodPatcher.fx_graph_module._copy_attr = self.copy_attr_new
        MagicMethodPatcher.fx_node._find_module_of_method = self.find_module_of_method_new

    def __exit__(self, exc_type, exc_value, tb):
        MagicMethodPatcher.fx_graph.magic_methods = MagicMethodPatcher.magic_methods_ori
        MagicMethodPatcher.fx_graph._format_target = MagicMethodPatcher.format_target_ori
        MagicMethodPatcher.fx_graph_module._copy_attr = MagicMethodPatcher.copy_attr_ori
        MagicMethodPatcher.fx_node._find_module_of_method = MagicMethodPatcher.find_module_of_method_ori
        return exc_type is None

def _create_wrapped_leaf_func(tracer: ConcreteTracer, func: Callable, to_func: Optional[Callable], init_tracers = ()):
    # to_func: to call correct replacement instead of the original (the original func may be wrong).
    #          such as: call torch.nn.norm instead of torch._C._VariableFunctions.norm.
    #                   torch.nn.norm will help to pack dim to list if dim is an int.
    if to_func is None:
        to_func = func
    @functools.wraps(func)
    def func_wrapper(*args, **kwargs):
        if tracer.temp_disable_call:
            return func(*args, **kwargs)
        tracers = _orig_set(init_tracers)
        def unwrap_detect_tracers(obj):
            if isinstance(obj, ep.ConcreteProxy):
                tracers.add(obj.tracer)
        ep.map_aggregate_not_proxy(args, unwrap_detect_tracers)
        ep.map_aggregate_not_proxy(kwargs, unwrap_detect_tracers)
        if _orig_len(tracers) == 0:
            return to_func(*args, **kwargs)
        elif _orig_len(tracers) == 1 and next(iter(tracers)) == tracer:
            return tracer.create_proxy('call_function', to_func, args, kwargs)
        else:
            raise Exception('more than 1 tracer detected. please report the issue')
    return func_wrapper

def _create_wrapped_leaf_method(tracer: ConcreteTracer, method, name: str, to_func: Optional[Callable]):
    @functools.wraps(method)
    def method_wrapper(*args, **kwargs):
        if tracer.temp_disable_call:
            return method(*args, **kwargs)
        tracers = _orig_set()
        def unwrap_detect_tracers(obj):
            if isinstance(obj, ep.ConcreteProxy):
                tracers.add(obj.tracer)
        ep.map_aggregate_not_proxy(args, unwrap_detect_tracers)
        ep.map_aggregate_not_proxy(kwargs, unwrap_detect_tracers)
        if _orig_len(tracers) == 0:
            return method(*args, **kwargs)
        elif _orig_len(tracers) == 1 and next(iter(tracers)) == tracer:
            if to_func is not None:
                return tracer.create_proxy('call_function', to_func, args, kwargs)
            else:
                return tracer.create_proxy('call_method', name, args, kwargs)
        else:
            raise Exception('more than 1 tracer detected. please report the issue')
    return method_wrapper

def _create_wrapped_leaf_class(tracer: ConcreteTracer, clz):
    class clz_wrapper_clz:
        @functools.wraps(clz)
        def __call__(self, *args, **kwargs):
            if tracer.temp_disable_call:
                return clz(*args, **kwargs)
            tracers = _orig_set()
            def unwrap_detect_tracers(obj):
                if isinstance(obj, ep.ConcreteProxy):
                    tracers.add(obj.tracer)
            ep.map_aggregate_not_proxy(args, unwrap_detect_tracers)
            ep.map_aggregate_not_proxy(kwargs, unwrap_detect_tracers)
            if _orig_len(tracers) == 0:
                return clz(*args, **kwargs)
            elif _orig_len(tracers) == 1 and next(iter(tracers)) == tracer:
                return tracer.create_proxy('call_function', clz, args, kwargs)
            else:
                raise Exception('more than 1 tracer detected. please report the issue')
        def __eq__(self, __o: object) -> bool:
            return id(__o) in (id(self), id(clz))
        def __hash__(self):
            return id(self)
    return clz_wrapper_clz()

def _create_wrapped_leaf_iterable_class(tracer: ConcreteTracer, clz):
    class clz_wrapper_clz:
        @functools.wraps(clz)
        def __call__(self, *args, **kwargs):
            if tracer.temp_disable_call:
                return clz(*args, **kwargs)
            tracers = _orig_set()
            if _orig_len(args) != 0:
                if _orig_isinstance(args[0], ep.Proxy):
                    tracers.add(args[0].tracer)
                if _orig_isinstance(args[0], Iterator):
                    args = (clz(args[0]), *args[1:])
                if _orig_isinstance(args[0], Iterable):
                    for item in args[0]:
                        if _orig_isinstance(item, ep.Proxy):
                            tracers.add(item.tracer)
            if _orig_len(tracers) == 0:
                return clz(*args, **kwargs)
            elif _orig_len(tracers) == 1 and next(iter(tracers)) == tracer:
                return tracer.create_proxy('call_function',
                    clz, args, kwargs)
            else:
                raise Exception('more than 1 tracer detected. please report the issue')
        def __eq__(self, __o: object) -> bool:
            return id(__o) in (id(self), id(clz))
        def __hash__(self):
            return id(self)
    clz_wrapper = clz_wrapper_clz()
    for name in dir(clz):
        attr = _orig_getattr(clz, name)
        if not name.startswith('_') or name in ('__getitem__', '__setitem__', '__iter__', '__len__'):
            if _orig_isinstance(attr, Callable):
                setattr(clz_wrapper, name, _create_wrapped_leaf_method(tracer, attr, name, None))
            else:
                setattr(clz_wrapper, name, attr)
    return clz_wrapper

def _create_wrapped_attr_for_middle_class(tracer: ConcreteTracer, clz, the_path_of_middle_class):
    _orig_clz_getattribute = clz.__getattribute__
    if hasattr(clz, '__getattr__'):
        _orig_clz_getattr = clz.__getattr__
    else:
        _orig_clz_getattr = None
    @functools.wraps(_orig_clz_getattribute)
    def clz_getattr_wrapper(obj, attr):
        if tracer.temp_disable_call | tracer.temp_disable_attr:
            if _orig_clz_getattr == None:
                return _orig_clz_getattribute(obj, attr)
            else:
                try:
                    return _orig_clz_getattribute(obj, attr)
                except AttributeError:
                    return _orig_clz_getattr(obj, attr)
        else:
            return tracer.create_proxy('get_attr', f'{the_path_of_middle_class[id(obj)]}.{attr}', (), {})
    return clz_getattr_wrapper

def concrete_trace(root : Union[torch.nn.Module, Callable[..., Any]],
                   concrete_args: Union[Dict[str, Any], Tuple],
                   *,
                   use_function_patch: bool = False,
                   function_patch_backlist: List[str] = [],
                   forwrad_function_name: str = 'forward',
                   check_args: Optional[Dict[str, Any]] = None,
                   autowrap_leaf_function = ConcreteTracer.default_autowrap_leaf_function,
                   autowrap_leaf_class = ConcreteTracer.default_autowrap_leaf_class,
                   leaf_module = (),
                   fake_middle_class = ()) -> GraphModule:
    """
    Concrete tracing API

    Given an ``nn.Module`` or function instance ``root`` and a dummy input `concrete_args`, this function will return a ``GraphModule``
    constructed by recording operations seen while tracing through ``root``.

    It has solved many problems compared to fx.symbolic_trace, and can execute on many third-party models.

    For example::

        def f(a, b):
            return a + b

        traced_f = concrete_trace(f, concrete_args={'a': 1, 'b': 2})
        # or `traced_f = concrete_trace(f, (1, 2))`
        assert traced_f(3, 4) == 7

        def f(x):
            out1, out2 = 0, 0
            for k, v in x.items():
                out1 += k
                out2 += v
            return out1, out2
        traced_f = concrete_trace(f, ({1: 1, 2: 2}, ))
        assert traced_f({2: 3, 4: 5}) == (6, 8)

    Note that we can only record static structure, so all the branches such as if-else or loop will be flattened::

        def f(x):
            out1, out2 = 0, 0
            for k, v in x.items():
                out1 += k
                out2 += v
            return out1, out2
        traced_f = concrete_trace(f, ({1: 1, 2: 2}, ))
        assert traced_f({2: 3, 4: 5, 6:7}) == (6, 8) # not (12, 15)

        # traced code like:
        def traced_f(self, x):
            out1, out2 = 0, 0
            items = x.items()

            # for loop
            iter = iter(items)

            # first loop content
            items0 = next(iter)
            out1 += items0[0]
            out2 += items0[1]

            # second loop content
            items1 = next(iter)
            out1 += items1[0]
            out2 += items1[1]

            return (out1, out2)

    If you want to trace 'is', 'is not', 'in' or 'not in' in your module, you can set use_function_patch to True::

        def f(x, y):
            if x is None:
                return y
            else:
                return x - y
        # traced_f = concrete_trace(f, (None, 1)) # bad
        traced_f = concrete_trace(f, (None, 1), use_function_patch=True) # f should exist in a file.

    If you have a function/method that should be treated as a leaf function but not trace into it, use autowrap_leaf_function to mark it::

        def leaf_op(x, y, z):
            # if not treated as a leaf function, then only 1 branch will exist.
            if x > 0:
                return y + z
            else:
                return y - z

        def f(x):
            return leaf_op(x, 3, 2)

        traced_f = concrete_trace(f, (1, ), autowrap_leaf_function = {
            leaf_op: ([], False, None), **ConcreteTracer.default_autowrap_leaf_function})
        assert traced_f(1) == 5 and traced_f(-1) == 1

    If you have a class that should be treated as a leaf class, use autowrap_leaf_class to mark it::

        class leaf_clz:
            def __init__(self, a, b):
                self.c = a + b

        def f(x, y):
            return leaf_clz(x, y)

        traced_f = concrete_trace(f, (1, 2), autowrap_leaf_class = {
            leaf_clz: ([], False), **ConcreteTracer.default_autowrap_leaf_class})
        assert isinstance(traced_f(3, 4), leaf_clz) and traced_f(3, 4).c == 7

    Args:
        root (Union[torch.nn.Module, Callable]): Module or function to be traced and converted into a Graph representation.
        concrete_args (Union[Dict[str, Any], Tuple]): Dummy inputs to do concrete trace.

        use_function_patch (bool): Use operator patcher recursively on function calls. Operator patcher will re-compile the function and
            translate '{} is {}' into 'operator.is_({}, {})', then we can treat 'is', 'is not', 'in' and 'not in' as function calls.

        function_patch_backlist (List[str]): Blacklist of the operator patcher.

        autowrap_leaf_function (Dict[Any, Tuple[List[Tuple[Union[ModuleType, Type], str]], bool, Optional[Callable]]]): Leaf function dict,
            such as 'add' or 'torch.xxx'. You can add your own leaf functions.

            The struct of dict is: leaf_function: ([(module_path, module_name)], force_to_trace, replace_to_function).
                (module_path, module_name): The place the function exists. Such as torch.meshgrid, there are `torch.meshgrid`,
                    'torch.functional.meshgrid', 'torch._C._VariableFunctions.meshgrid', we should wrap them all.
                force_to_trace: If set to false, the function will only be traced if input relates to concrete_args.
                    Such as 'torch.rand', we should trace it even if it doesn't relate to concrete_args.
                replace_to_function: If not `None`, we will use it to replace the original function in traced code.
                    Such as ModuleList.__getitem__, we can use operator.getitem to replace it.

        default_autowrap_leaf_class (Dict[Type, Tuple[List[Tuple[Union[ModuleType, Type], str]], bool]]): Leaf class dict, such as 'int',
            'range' or 'zip'. You can add your own leaf functions such as 'torch.finfo' or 'modeling_outputs.SequenceClassifierOutput'.

            The struct of dict is: leaf_class: ([(module_path, module_name)], is_iterator_class).
                is_iterator_class: Is the class init from an iterator. Only 'tuple', 'list', 'set' or 'dict' needs to set it to True.

    Returns:
        fx.GraphModule: a Module created from the recorded operations from ``root``.
    """
    tracer = ConcreteTracer(
        autowrap_leaf_function = autowrap_leaf_function,
        autowrap_leaf_class = autowrap_leaf_class,
        leaf_module = leaf_module,
        fake_middle_class = fake_middle_class,
    )
    graph = tracer.trace(root, concrete_args, use_function_patch, function_patch_backlist, forwrad_function_name)
    graph_check = tracer.trace(root, concrete_args, use_function_patch, function_patch_backlist, forwrad_function_name)
    # compare to check equal
    assert len(graph.nodes) == len(graph_check.nodes)
    for node_a, node_b in zip(graph.nodes, graph_check.nodes):
        node_a: Node
        node_b: Node
        target_a = node_a.target
        target_b = node_b.target
        if node_a.op == 'get_attr' and node_a.name.startswith('_tensor_constant'):
            assert node_b.op == 'get_attr' and node_b.name.startswith('_tensor_constant')
            assert torch.equal(getattr(root, node_a.name), getattr(root, node_b.name))
        elif node_a.op == 'call_function' and isinstance(target_a, Callable) and target_a.__name__ == 'apply' and\
            hasattr(target_a, '__self__') and issubclass(target_a.__self__, torch.autograd.Function):
            assert node_b.op == 'call_function' and isinstance(target_b, Callable) and target_b.__name__ == 'apply' and\
            hasattr(target_b, '__self__') and issubclass(target_b.__self__, torch.autograd.Function)
        else:
            assert node_a.op == node_b.op and target_a == target_b

    with MagicMethodPatcher(tracer.root):
        name = root.__class__.__name__ if isinstance(root, torch.nn.Module) else root.__name__
        traced = GraphModule(tracer.root, graph, name)

    # TODO: better infomation
    # # assert root(**concrete_args) == traced(**concrete_args)
    if check_args is not None:
         assert root(**check_args) == traced(**check_args)
    return traced
