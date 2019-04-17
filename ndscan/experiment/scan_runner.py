"""Generic scanning loop.

While :mod:`.scan_generator` describes a scan to be run in the abstract, this module
contains the implementation to actually execute one within an ARTIQ experiment. This
will likely be used by end users via ``FragmentScanExperiment`` or subscans.
"""

from artiq.language import *
from contextlib import suppress
from itertools import islice
from typing import Any, Dict, List, Iterator, Tuple
from .default_analysis import AnnotationContext, DefaultAnalysis
from .fragment import ExpFragment
from .parameters import ParamStore, type_string_to_param
from .result_channels import ResultChannel, ResultSink
from .scan_generator import generate_points, ScanGenerator, ScanOptions
from .utils import is_kernel


class ScanFinished(Exception):
    """Used internally to signal that a scan has been successfully completed (points
    exhausted).

    This is a kludge to work around a bug where tuples of empty lists crash the ARTIQ
    RPC code (kernel aborts), and should never be visible to the user.
    """
    pass


class ScanAxis:
    """Describes a single axis that is being scanned.

    Apart from the metadata, this also includes the necessary information to execute the
    scan at runtime; i.e. the :class:`.ParamStore` to modify in order to set the
    parameter.
    """

    def __init__(self, param_schema: Dict[str, Any], path: str,
                 param_store: ParamStore):
        self.param_schema = param_schema
        self.path = path
        self.param_store = param_store


class ScanSpec:
    """Describes a single scan.

    :param axes: The list of parameters that are scanned.
    :param generators: Generators that give the points for each of the specified axes.
    :param options: Applicable :class:`.ScanOptions`.
    """

    def __init__(self, axes: List[ScanAxis], generators: List[ScanGenerator],
                 options: ScanOptions):
        self.axes = axes
        self.generators = generators
        self.options = options


class ScanRunner(HasEnvironment):
    """Runs the actual loop that executes an :class:`.ExpFragment` for a specified list
    of scan axes (on either the host or core device, as appropriate).

    Integrates with the ARTIQ scheduler to pause/terminate execution as requested.

    Conceptually, this is only a single function (``run()``), but is wrapped in a class
    to follow the idiomatic ARTIQ kernel/``HasEnvironment`` integration style.
    """

    # Note: ARTIQ Python is currently severely limited in its support for generics or
    # metaprogramming. While the interface for this class is effortlessly generic, the
    # implementation might well be a long-forgotten ritual for invoking Cthulhu, and is
    # special-cased for a number of low dimensions.

    def build(self):
        ""
        self.setattr_device("core")
        self.setattr_device("scheduler")

    def run(self, fragment: ExpFragment, spec: ScanSpec,
            axis_sinks: List[ResultSink]) -> None:
        """Run a scan of the given fragment, with axes as specified.

        :param fragment: The fragment to iterate.
        :param options: The options for the scan generator.
        :param axis_sinks: A list of :class:`.ResultSink` instances to push the
            coordinates for each scan point to, matching ``scan.axes``.
        """

        # Stash away _fragment in member variable to pacify ARTIQ compiler; there is no
        # reason this shouldn't just be passed along and materialised as a global.
        self._fragment = fragment

        points = generate_points(spec.generators, spec.options)

        # TODO: Support parameters which require host_setup() when changed.
        run_impl = self._run_scan_on_core_device if is_kernel(
            self._fragment.run_once) else self._run_scan_on_host
        run_impl(points, spec.axes, axis_sinks)

    def _run_scan_on_host(self, points: Iterator[Tuple], axes: List[ScanAxis],
                          axis_sinks: List[ResultSink]) -> None:
        while True:
            axis_values = next(points, None)
            if axis_values is None:
                break
            for (axis, value, sink) in zip(axes, axis_values, axis_sinks):
                axis.param_store.set_value(value)
                sink.push(value)

            self._fragment.host_setup()
            self._fragment.device_setup()
            self._fragment.run_once()
            self.scheduler.pause()

    def _run_scan_on_core_device(self, points: list, axes: List[ScanAxis],
                                 axis_sinks: List[ResultSink]) -> None:
        # Set up members to be accessed from the kernel through the
        # _kscan_param_values_chunk RPC call later.
        self._kscan_points = points
        self._kscan_axes = axes
        self._kscan_axis_sinks = axis_sinks

        # Stash away points in current kernel chunk until they have been marked
        # complete so we can resume from interruptions.
        self._kscan_current_chunk = []

        # Interval between scheduler.check_pause() calls on the core device (or rather,
        # the minimum interval; calls are only made after a point has been completed).
        self._kscan_pause_check_interval_mu = self.core.seconds_to_mu(0.2)

        for i, axis in enumerate(axes):
            setattr(self, "_kscan_param_setter_{}".format(i),
                    axis.param_store.set_value)

        # _kscan_param_values_chunk returns a tuple of lists of values, one for each
        # scan axis. Synthesize a return type annotation (`def foo(self): -> …`) with
        # the concrete type for this scan so the compiler can infer the types in
        # _kscan_impl() correctly.
        self._kscan_param_values_chunk.__func__.__annotations__ = {
            "return":
            TTuple([
                TList(type_string_to_param(a.param_schema["type"]).CompilerType)
                for a in axes
            ])
        }

        # FIXME: Replace this with generated code once eval_kernel() is implemented.
        num_dims = len(axes)
        scan_impl = getattr(self, "_kscan_impl_{}".format(num_dims), None)
        if scan_impl is None:
            raise NotImplementedError(
                "{}-dimensional scans not supported yet".format(num_dims))

        with suppress(ScanFinished):
            self._kscan_update_host_param_stores()
            while True:
                self._fragment.host_setup()
                scan_impl()
                self.core.comm.close()
                self.scheduler.pause()

    @kernel
    def _kscan_impl_1(self):
        last_pause_check_mu = self.core.get_rtio_counter_mu()
        while True:
            (param_values_0, ) = self._kscan_param_values_chunk()
            for i in range(len(param_values_0)):
                self._kscan_param_setter_0(param_values_0[i])
                self._kscan_run_fragment_once()
                self._kscan_point_completed()

                current_time_mu = self.core.get_rtio_counter_mu()
                if (current_time_mu - last_pause_check_mu >
                        self._kscan_pause_check_interval_mu):
                    if self.scheduler.check_pause():
                        return
                    last_pause_check_mu = current_time_mu

    @kernel
    def _kscan_impl_2(self):
        last_pause_check_mu = self.core.get_rtio_counter_mu()
        while True:
            param_values_0, param_values_1 = self._kscan_param_values_chunk()
            for i in range(len(param_values_0)):
                self._kscan_param_setter_0(param_values_0[i])
                self._kscan_param_setter_1(param_values_1[i])
                self._kscan_run_fragment_once()
                self._kscan_point_completed()

                current_time_mu = self.core.get_rtio_counter_mu()
                if (current_time_mu - last_pause_check_mu >
                        self._kscan_pause_check_interval_mu):
                    if self.scheduler.check_pause():
                        return
                    last_pause_check_mu = current_time_mu

    @kernel
    def _kscan_impl_3(self):
        last_pause_check_mu = self.core.get_rtio_counter_mu()
        while True:
            param_values_0, param_values_1, param_values_2 =\
                self._kscan_param_values_chunk()
            for i in range(len(param_values_0)):
                self._kscan_param_setter_0(param_values_0[i])
                self._kscan_param_setter_1(param_values_1[i])
                self._kscan_param_setter_2(param_values_2[i])
                self._kscan_run_fragment_once()
                self._kscan_point_completed()

                current_time_mu = self.core.get_rtio_counter_mu()
                if (current_time_mu - last_pause_check_mu >
                        self._kscan_pause_check_interval_mu):
                    if self.scheduler.check_pause():
                        return
                    last_pause_check_mu = current_time_mu

    @kernel
    def _kscan_run_fragment_once(self):
        self._fragment.device_setup()
        self._fragment.run_once()

    def _kscan_param_values_chunk(self):
        # Number of scan points to send at once. After each chunk, the kernel needs to
        # execute a blocking RPC to fetch new points, so this should be chosen such
        # that latency/constant overhead and throughput are balanced. 10 is an arbitrary
        # choice based on the observation that even for fast experiments, 10 points take
        # a good fraction of a second, while it is still low enough not to run into any
        # memory management issues on the kernel.
        CHUNK_SIZE = 10

        self._kscan_current_chunk.extend(
            islice(self._kscan_points, CHUNK_SIZE - len(self._kscan_current_chunk)))

        values = tuple([] for _ in self._kscan_axes)
        for p in self._kscan_current_chunk:
            for i, (value, axis) in enumerate(zip(p, self._kscan_axes)):
                # KLUDGE: Explicitly coerce value to the target type here so we can use
                # the regular (float) scans for integers until proper support for int
                # scans is implemented.
                values[i].append(axis.param_store.coerce(value))
        if not values[0]:
            raise ScanFinished
        return values

    @rpc(flags={"async"})
    def _kscan_point_completed(self):
        values = self._kscan_current_chunk.pop(0)
        for value, sink in zip(values, self._kscan_axis_sinks):
            sink.push(value)

        # TODO: Warn if some result channels have not been pushed to.

        self._kscan_update_host_param_stores()

    @host_only
    def _kscan_update_host_param_stores(self):
        """Set host-side parameter stores for the scan axes to their current values,
        i.e. as specified by the next point in the current scan chunk.

        This ensures that if a parameter is scanned from a kernel scan that requires
        a host RPC to update (e.g. a non-@kernel device_setup()), the RPC'd code will
        execute using the expected values.
        """

        # Generate the next set of values if we are at a chunk boundary.
        if not self._kscan_current_chunk:
            try:
                self._kscan_param_values_chunk()
            except ScanFinished:
                return
        # Set the host-side parameter stores.
        next_values = self._kscan_current_chunk[0]
        for value, axis in zip(next_values, self._kscan_axes):
            axis.param_store.set_value(value)


def filter_default_analyses(fragment: ExpFragment,
                            spec: ScanSpec) -> List[DefaultAnalysis]:
    """Return the default analyses of the given fragment that can be executed for the
    given scan spec."""
    result = []
    axis_identities = [(s.param_schema["fqn"], s.path) for s in spec.axes]
    for analysis in fragment.get_default_analyses():
        if analysis.has_data(axis_identities):
            result.append(analysis)
    return result


def describe_scan(spec: ScanSpec, fragment: ExpFragment,
                  short_result_names: Dict[ResultChannel, str]):
    """Return metadata for the given spec in stringly typed dictionary form, executing
    any default analyses as necessary.

    :param spec: :class:`.ScanSpec` describing the scan.
    :param fragment: Fragment being scanned.
    :param short_result_names: Map from result channel objects to shortened names.
    """
    desc = {}

    desc["fragment_fqn"] = fragment.fqn
    axis_specs = [{
        "param": ax.param_schema,
        "path": ax.path,
    } for ax in spec.axes]
    for ax, gen in zip(axis_specs, spec.generators):
        gen.describe_limits(ax)

    desc["axes"] = axis_specs
    desc["seed"] = spec.options.seed
    desc["channels"] = {
        name: channel.describe()
        for (channel, name) in short_result_names.items()
    }

    axis_identities = [(s.param_schema["fqn"], s.path) for s in spec.axes]
    context = AnnotationContext(
        lambda handle: axis_identities.index(handle._store.identity), lambda channel:
        short_result_names[channel])

    desc["annotations"] = []
    desc["online_analyses"] = {}
    for analysis in filter_default_analyses(fragment, spec):
        annotations, online_analyses = analysis.describe_online_analyses(context)
        desc["annotations"].extend(annotations)
        for name, spec in online_analyses.items():
            if name in desc["online_analyses"]:
                raise ValueError(
                    "An online analysis with name '{}' already exists".format(name))
            desc["online_analyses"][name] = spec

    return desc