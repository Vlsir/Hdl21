"""
# hdl21 Elaboration 

Defines the primary `elaborate` method used to flesh out an in-memory `Module` or `Generator`. 
Internally defines and uses a number of hierarchical visitor-classes which traverse the hardware hierarchy, 
performing one or more transformation-passes.  
"""
import copy
from enum import Enum, auto
from types import SimpleNamespace
from typing import Union, Any, Dict, List, Optional
from pydantic.dataclasses import dataclass

# Local imports
from .module import Module, ExternalModuleCall
from .instance import Instance, PortRef
from .primitives import PrimitiveCall
from .interface import Interface, InterfaceInstance
from .signal import PortDir, Signal, Visibility
from .generator import Generator, GeneratorCall
from .params import _unique_name


class Context:
    """Elaboration Context"""

    ...  # To be continued!


# Type short-hand for elaborate-able types
Elabable = Union[Module, GeneratorCall]
# (Plural Version)
Elabables = Union[Elabable, List[Elabable], SimpleNamespace]


def elabable(obj: Any) -> bool:
    # Function to test this, since `isinstance` doesn't work for `Union`.
    return isinstance(obj, (Module, Generator, GeneratorCall))


class _Elaborator:
    """ Base Elaborator Class """

    @classmethod
    def elaborate(cls, top, ctx):
        """ Elaboration entry-point. Elaborate the top-level object. """
        return cls(top, ctx).elaborate_top()

    def __init__(self, top: Elabable, ctx: Context):
        self.top = top
        self.ctx = ctx

    def elaborate_top(self):
        """ Elaborate our top node """
        if not isinstance(self.top, Module):
            raise TypeError
        return self.elaborate_module(self.top)

    def elaborate_generator_call(self, call: GeneratorCall) -> Module:
        """ Elaborate a GeneratorCall """
        # Only the generator-elaborator can handle generator calls; default it to error on others.
        raise RuntimeError(f"Invalid call to elaborate GeneratorCall by {self}")

    def elaborate_external_module(self, call: ExternalModuleCall) -> ExternalModuleCall:
        """ Elaborate an ExternalModuleCall """
        # Default: nothing to see here, carry on
        return call

    def elaborate_primitive_call(self, call: PrimitiveCall) -> PrimitiveCall:
        """ Elaborate a PrimitiveCall """
        # Default: nothing to see here, carry on
        return call

    def elaborate_interface_instance(self, inst: InterfaceInstance) -> None:
        """ Elaborate an InterfaceInstance """
        # Annotate each InterfaceInstance so that its pre-elaboration `PortRef` magic is disabled.
        inst._elaborated = True

    def elaborate_interface(self, intf: Interface) -> Interface:
        """ Elaborate an Interface """
        # Default: nothing to see here, carry on
        return intf

    def elaborate_instance(self, inst: Instance) -> Union[Module, PrimitiveCall]:
        """ Elaborate a Module Instance. """
        # This version of `elaborate_instance` is the "post-generators" version used by *most* passes.
        # The Generator-elaborator is different, and overrides it.

        inst._elaborated = True
        if not inst._resolved:
            raise RuntimeError(f"Error elaborating undefined Instance {inst}")
        if isinstance(inst._resolved, Module):
            return self.elaborate_module(inst._resolved)
        if isinstance(inst._resolved, PrimitiveCall):
            return self.elaborate_primitive_call(inst._resolved)
        if isinstance(inst._resolved, ExternalModuleCall):
            return self.elaborate_external_module(inst._resolved)
        raise TypeError

    @staticmethod
    def flatname(segments: List[str], *, avoid: dict, maxlen: int = 511) -> str:
        """ Create a attribute-name merging string-list `segments`, while avoiding all keys in dictionary `avoid`.
        Commonly re-used while flattening  nested objects and while creating explicit attributes from implicit ones. 
        Raises a `RunTimeError` if no such name can be found of length less than `maxlen`. 
        The default max-length is 511 characters, a value representative of typical limits in target EDA formats. """

        # The default format and result is of the form "_seg0_seg1_".
        # If that is (somehow) in the avoid-keys, append and prepend underscores until it's not.
        name = "_" + "_".join(segments) + "_"
        while True:
            if len(name) > maxlen:
                msg = f"Could not generate a flattened name for {segments}: (trying {name})"
                raise RuntimeError(msg)
            if name not in avoid:  # Done!
                break
            name = "_" + name + "_"  # Collision; append and prepend underscores
        return name


class GeneratorElaborator(_Elaborator):
    """ Hierarchical Generator Elaborator
    Walks a hierarchy from `top` calling Generators. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.generator_calls = dict()  # GeneratorCalls to their (Module) results
        self.modules = dict()  # Module ids to references
        self.primitive_calls = dict()  # PrimitiveCall ids to references
        self.ext_module_calls = dict()  # PrimitiveCall ids to references

    def elaborate_top(self):
        """ Elaborate our top node """
        if isinstance(self.top, Module):
            return self.elaborate_module(self.top)
        if isinstance(self.top, GeneratorCall):
            return self.elaborate_generator_call(self.top)
        raise TypeError(
            f"Invalid Elaboration top-level {self.top}, must be a Module or Generator"
        )

    def elaborate_generator_call(self, call: GeneratorCall) -> Module:
        """ Elaborate Generator-function-call `call`. Returns the generated Module. """

        # First check out cache
        if call in self.generator_calls:  # Already done!
            # Give the `call` a reference to its result.
            # Note this *has not* necessarily already happened, as the `self.generator_calls` key may be an equally-valued (but distinct) `GeneratorCall`.
            result = self.generator_calls[call]
            call.result = result
            return result

        # The main event: Run the generator-function
        if call.gen.usecontext:
            m = call.gen.func(call.arg, self.ctx)
        else:
            m = call.gen.func(call.arg)

        # Type-check the result
        # Generators may return other (potentially nested) generator-calls; unwind any of them
        while isinstance(m, GeneratorCall):
            # Note this should hit Python's recursive stack-check if it doesn't terminate
            m = self.elaborate_generator_call(m)
        # Ultimately they've gotta resolve to Modules, or they fail.
        if not isinstance(m, Module):
            raise TypeError(
                f"Generator {call.gen.func.__name__} returned {type(m)}, must return Module."
            )

        # Give the GeneratorCall a reference to its result, and store it in our local dict
        call.result = m
        self.generator_calls[call] = m
        # Create a unique name
        # If the Module that comes back is anonymous, start by giving it a name equal to the Generator's
        if m.name is None:
            m.name = call.gen.func.__name__
        # Then add a unique suffix per its parameter-values
        m.name += "(" + _unique_name(call.arg) + ")"
        # Update the Module's `pymodule`, which generally at this point is `hdl21.generator`
        m._pymodule = call.gen.pymodule
        # And elaborate the module
        return self.elaborate_module(m)

    def elaborate_module(self, module: Module) -> Module:
        """Elaborate Module `module`. First depth-first elaborates its Instances,
        before creating any implicit Signals and connecting them.
        Finally checks for connection-consistency with each Instance."""
        if id(module) in self.modules:  # Already done!
            return module

        if not module.name:
            raise RuntimeError(
                f"Anonymous Module {module} cannot be elaborated (did you forget to name it?)"
            )
        # Depth-first traverse instances, ensuring their targets are defined
        for inst in module.instances.values():
            self.elaborate_instance(inst)
        # Also visit interface instances, turning off their pre-elab magic
        for inst in module.interfaces.values():
            self.elaborate_interface_instance(inst)

        # Store a reference to the now-expanded Module in our cache, and return it
        self.modules[id(module)] = module
        return module

    def elaborate_instance(self, inst: Instance) -> Module:
        """ Elaborate a Module Instance.
        Largely pushes through depth-first definition of the target-Module.
        Connections, port-direction checking and the like are performed in `elaborate_module`. """

        # Turn off the instance's pre-elaboration magic
        inst._elaborated = True

        # And call its type-specific elaboration method
        if isinstance(inst.of, GeneratorCall):
            return self.elaborate_generator_call(call=inst.of)
        if isinstance(inst.of, Module):
            return self.elaborate_module(inst.of)
        if isinstance(inst.of, PrimitiveCall):
            return self.elaborate_primitive_call(inst.of)
        if isinstance(inst.of, ExternalModuleCall):
            return self.elaborate_external_module(inst.of)
        raise TypeError(f"Invalid Instance of {inst.of}")


class ImplicitSignals(_Elaborator):
    """ Explicitly create any implicitly-defined `Signal`s, 
    e.g. those defined by port-to-port connections. """

    def elaborate_module(self, module: Module) -> Module:
        """Elaborate Module `module`. First depth-first elaborates its Instances,
        before creating any implicit Signals and connecting them."""

        # FIXME: much of this can and should be shared with `ImplicitInterfaces`

        # Interfaces must be flattened before this point.
        # Throw an error if not.
        if len(module.interfaces):
            raise RuntimeError(
                f"ImplicitSignals elaborator invalidly invoked on Module {module} with Interfaces {module.interfaces}"
            )

        # Depth-first traverse instances, ensuring their targets are defined
        for inst in module.instances.values():
            self.elaborate_instance(inst)

        # Now work through expanding any implicit-ish connections, such as those from port to port.
        # Start by building an adjacency graph of every `PortRef` that's been instantiated
        portconns = dict()
        for inst in module.instances.values():
            for port, conn in inst.conns.items():
                if isinstance(conn, PortRef):
                    internal_ref = PortRef(inst, port)
                    portconns[internal_ref] = conn

        # Now walk through them, assigning each set to a net
        nets: List[List[PortRef]] = list()
        while portconns:
            # Keep both a list for consistent ordering (we think), and a set for quick membership tests
            this_net = SetList()
            # Grab a random pair
            inner, outer = portconns.popitem()
            this_net.add(inner)
            this_net.add(outer)
            # If it's connected to others, grab those
            while outer in portconns:
                outer = portconns.pop(outer)
                this_net.add(outer)
            # Once we find an object not in `portconns`, we've covered everything connected to the net.
            nets.append(this_net.order)

        # For each net, find and/or create a Signal to replace all the PortRefs with.
        for net in nets:
            # Check whether any of them are connected to declared Signals.
            # And if any are, make sure there's only one
            sig = None
            for portref in net:
                portconn = portref.inst.conns.get(portref.portname, None)
                if isinstance(portconn, Signal):
                    if sig is not None and portconn is not sig:
                        # Ruh roh! shorted between things
                        raise RuntimeError(
                            f"Invalid connection to {portconn}, shorting {[(p.inst.name, p.portname) for p in net]}"
                        )
                    sig = portconn
            # If we didn't find any, go about naming and creating one
            if sig is None:
                # Find a unique name for the new Signal.
                # The default name template is "_inst1_port1_inst2_port2_inst3_port3_"
                signame = self.flatname(
                    segments=[f"{p.inst.name}_{p.portname}" for p in net],
                    avoid=module.namespace,
                )
                # Create the Signal, looking up all its properties from the last Instance's Module
                # (If other instances are inconsistent, later stages will flag them)
                lastmod = portref.inst._resolved
                sig = lastmod.ports.get(portref.portname, None)
                if sig is not None:  # Clone it, and remove any Port-attributes
                    sig = copy.copy(sig)
                    sig.vis = Visibility.INTERNAL
                    sig.direction = PortDir.NONE
                else:
                    raise RuntimeError(
                        f"Invalid port {portref.portname} on Instance {portref.inst.name} in Module {module.name}"
                    )
                # Rename it and add it to the Module namespace
                sig.name = signame
                module.add(sig)
            # And re-connect it to each Instance
            for portref in net:
                portref.inst.conns[portref.portname] = sig

        return module


@dataclass
class FlatInterfaceInst:
    """ Flattened Hierarchical Interface, resolved to constituent Signals """

    inst_name: str  # Interface Instance name
    src: Interface  # Source/ Original Interface
    # Flattened signals-dict, keyed by *original* signal name
    signals: Dict[str, Signal]


class InterfaceFlattener(_Elaborator):
    """ Interface-Flattening Elaborator Pass """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.modules = dict()
        self.replacements = dict()  # Interface replacement-dicts by Module (id)

    def elaborate_module(self, module: Module) -> Module:
        """ Flatten Module `module`s Interfaces, replacing them with newly-created Signals.
        Reconnect the flattened Signals to any Instances connected to said Interfaces. """
        if id(module) in self.modules:
            return module  # Already done!

        # Depth-first traverse our instances first.
        # After this loop, each Instance's Module can have newly-expanded ports -
        # but will not have modified its `conns` dict.
        for inst in module.instances.values():
            self.elaborate_instance(inst)

        # Start a dictionary of interface-replacements
        replacements = dict()

        while module.interfaces:
            # Remove the interface-instance from the module
            name, intf = module.interfaces.popitem()
            module.namespace.pop(name)
            # Flatten it
            flat = self.flatten_interface(intf)
            replacements[name] = flat

            # And add each flattened Signal
            for sig in flat.signals.values():
                signame = self.flatname([intf.name, sig.name], avoid=module.namespace)
                if intf.port:
                    vis_ = Visibility.PORT
                    if intf.role is None:
                        dir_ = PortDir.NONE
                    elif intf.role == sig.src:
                        dir_ = PortDir.OUTPUT
                    elif intf.role == sig.dest:
                        dir_ = PortDir.INPUT
                    else:
                        dir_ = PortDir.NONE
                else:
                    vis_ = Visibility.INTERNAL
                    dir_ = PortDir.NONE
                # Apply all these attributes to our new Signal
                sig.name = signame
                sig.vis = vis_
                sig.direction = dir_
                # And add it to the Module namespace
                module.add(sig)

            # Replace connections to any connected instances
            for inst in module.instances.values():
                for portname in list(inst.conns.keys()):
                    if inst.conns[portname] is intf:
                        inst_replacements = self.replacements[id(inst._resolved)]
                        inst_flat = inst_replacements[portname]
                        for flatname, flatsig in flat.signals.items():
                            inst_signame = inst_flat.signals[flatname].name
                            inst.conns[inst_signame] = flatsig
                        # And remove the connection to the original Interface(s)
                        inst.conns.pop(portname)

            # Re-connect any PortRefs it has given out, as in the form:
            # i = SomeInterface()    # Theoretical Interface with signal-attribute `s`
            # x = SomeModule(s=i.s)  # Connects `PortRef` `i.s`
            # FIXME: this needs to happen for hierarchical interfaces too
            for portref in intf.portrefs.values():
                flatsig = flat.signals.get(portref.portname, None)
                if flatsig is None:
                    raise RuntimeError(
                        f"Port {portref.portname} not found in Interface {intf.of.name}"
                    )
                # Walk through our Instances, replacing any connections to this `PortRef` with `flatsig`
                for inst in module.instances.values():
                    for portname, conn in inst.conns.items():
                        if conn is portref:
                            inst.conns[portname] = flatsig

        self.modules[id(module)] = module
        self.replacements[id(module)] = replacements
        return module

    def flatten_interface(self, intf: InterfaceInstance) -> FlatInterfaceInst:
        """ Convert nested Interface `intf` into a flattened `FlatInterfaceInst` of scalar Signals. 
        Flattening and the underlying Signal-cloning is applied to each Interface *instance*, 
        so each Module-visit need not re-clone the signals of the returned `FlatInterfaceInst`. """

        # Create the flattened version, initializing it with `intf`s scalar Signals
        flat = FlatInterfaceInst(
            inst_name=intf.name, src=intf.of, signals=copy.deepcopy(intf.of.signals)
        )
        # Depth-first walk any interface-instance's definitions, flattening them
        for i in intf.of.interfaces.values():
            iflat = self.flatten_interface(i)
            for sig in iflat.signals.values():
                # Rename the signal, and store it in our flat-intf
                signame = self.flatname([i.name, sig.name], avoid=flat.signals)
                sig.name = signame
                flat.signals[signame] = sig
        return flat


class ImplicitInterfaces(_Elaborator):
    """ Create explicit `InterfaceInstance`s for any implicit ones, 
    i.e. those created through port-to-port connections. """

    def elaborate_module(self, module: Module) -> Module:
        """ Elaborate Module `module`. First depth-first elaborates its Instances,
        before creating any implicit `InterfaceInstance`s and connecting them. """

        # FIXME: much of this can and should be shared with `ImplicitSignals`

        # Depth-first traverse instances, ensuring their targets are defined
        for inst in module.instances.values():
            self.elaborate_instance(inst)

        # Now work through expanding any implicit-ish connections, such as those from port to port.
        # Start by building an adjacency graph of every interface-valued `PortRef` that's been instantiated.
        portconns = dict()
        for inst in module.instances.values():
            for port, conn in inst.conns.items():
                if isinstance(conn, PortRef) and isinstance(
                    conn.inst._resolved, (Module, Interface)
                ):
                    other_port = conn.inst._resolved.get(conn.portname)
                    if other_port is None:
                        raise RuntimeError
                    if isinstance(other_port, InterfaceInstance):
                        internal_ref = PortRef(inst, port)
                        portconns[internal_ref] = conn

        # Now walk through them, assigning each set to a net
        nets: List[List[PortRef]] = list()
        while portconns:
            # Keep both a list for consistent ordering (we think), and a set for quick membership tests
            this_net = SetList()
            # Grab a random pair
            inner, outer = portconns.popitem()
            this_net.add(inner)
            this_net.add(outer)
            # If it's connected to others, grab those
            while outer in portconns:
                outer = portconns.pop(outer)
                this_net.add(outer)
            # Once we find an object not in `portconns`, we've covered everything connected to the net.
            nets.append(this_net.order)

        # For each net, find and/or create a Signal to replace all the PortRefs with.
        for net in nets:
            # Check whether any of them are connected to declared Signals.
            # And if any are, make sure there's only one
            sig = None
            for portref in net:
                portconn = portref.inst.conns.get(portref.portname, None)
                if isinstance(portconn, InterfaceInstance):
                    if sig is not None and portconn is not sig:
                        # Ruh roh! shorted between things
                        raise RuntimeError(
                            f"Invalid connection to {portconn}, shorting {[(p.inst.name, p.portname) for p in net]}"
                        )
                    sig = portconn
            # If we didn't find any, go about naming and creating one
            if sig is None:
                # Find a unique name for the new Signal.
                # The default name template is "_inst1_port1_inst2_port2_inst3_port3_"
                signame = self.flatname(
                    segments=[f"{p.inst.name}_{p.portname}" for p in net],
                    avoid=module.namespace,
                )
                # Create the Signal, looking up all its properties from the last Instance's Module
                # (If other instances are inconsistent, later stages will flag them)
                lastmod = portref.inst._resolved
                sig = lastmod._interface_ports.get(portref.portname, None)
                if sig is not None:
                    sig = copy.copy(sig)
                    sig.port = False
                    sig.role = None
                else:
                    raise RuntimeError(
                        f"Invalid port {portref.portname} on Instance {portref.inst.name} in Module {module.name}"
                    )
                # Rename it and add it to the Module namespace
                sig.name = signame
                module.add(sig)
            # And re-connect it to each Instance
            for portref in net:
                portref.inst.conns[portref.portname] = sig

        return module


class SetList:
    """ A common combination of a hash-set and ordered list of the same items. 
    Used for keeping ordered items while maintaining quick membership testing. """

    def __init__(self):
        self.set = set()
        self.list = list()

    def __contains__(self, item):
        return item in self.set

    def add(self, item):
        if item not in self.set:
            self.set.add(item)
            self.list.append(item)

    @property
    def order(self):
        return self.list


class ElabPasses(Enum):
    """ Enumerated Elaborator Passes
    Each has a `value` attribute which is an Elaborator-pass, 
    and a `name` attribute which is a (Python-enum-style) capitalized name. 
    Specifying  """

    RUN_GENERATORS = GeneratorElaborator
    IMPLICIT_INTERFACES = ImplicitInterfaces
    FLATTEN_INTERFACES = InterfaceFlattener
    IMPLICIT_SIGNALS = ImplicitSignals


def elab_all(top: Elabables, **kwargs) -> List[Elabable]:
    """ Elaborate everything we can find - potentially recursively - in `Elabables` `top`. 

    Results are returned in a list, not necessarily reproducing the structure of `top`. 
    Note the *attributes* of `top` are also generally modified in-place, allowing access to their elaboration results. """
    # Recursively create a list of all elab-able types in `obj`
    ls = []
    _list_elabables_helper(top, ls)
    # Elaborate each, and return them as a list
    return [elaborate(top=t, **kwargs) for t in ls]


def _list_elabables_helper(obj: Any, accum: list) -> None:
    """ Recursive helper for hierarchically finding elaborate-able things in `obj`. 
    Newly-found items are appended to accumulation-list `accum`. """
    if elabable(obj):
        accum.append(obj)
    elif isinstance(obj, list):
        [_list_elabables_helper(i, accum) for i in obj]
    elif isinstance(obj, SimpleNamespace):
        # Note this skips over non-elaboratable items (e.g. names), where the list demands all be suitable.
        for i in obj.__dict__.values():
            if isinstance(i, (SimpleNamespace, list)) or elabable(i):
                _list_elabables_helper(i, accum)
    else:
        raise TypeError(f"Attempting Invalid Elaboration of {obj}")


def elaborate(
    top: Elabable,
    *,
    ctx: Optional[Context] = None,
    passes: Optional[List[ElabPasses]] = None,
) -> Module:
    """ In-Memory Elaboration of Generator or Module `top`. 
    
    Optional `passes` lists the ordered `ElabPass`es to run. By default it runs all passes in the `ElabPass` enumeration, in definition order. 
    Note the order of passes is important; many depend upon others to have completed before they can successfully run. 

    Optional `Context` field `ctx` is not yet supported. 

    `elaborate` executes elaboration of a *single* `top` object. 
    For (plural) combinations of `Elabable` objects, use `elab_all`. 
    """
    # Expand default values
    ctx = ctx or Context()
    passes = passes or ElabPasses

    # Pass `top` through each of our passes, in order
    res = top
    for pass_ in passes:
        res = pass_.value.elaborate(top=res, ctx=ctx)
    return res
