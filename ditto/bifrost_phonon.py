"""Build BIFROST with a lead-phonon sample and write what the simulation pipeline needs.

Running this file writes

    bifrost.instr.json  the sample-loaded instrument as a serialized mccode-antlr Instr,
                        which is what the simulation commands run
    bifrost.json        NeXus Structure JSON for the file-writer, from a *vanilla* BIFROST
    bifrost-instr/      the sample-loaded instrument as a `.instr` file per included section
    bifrost.efu_calibration.json
                        the EFU's charge-division calibration, expected from the
                        resistances of the simulated triplets
    bifrost_cbm.json    the beam-monitor (cbm) EFU's configuration, from niess
    bifrost.senders.json
                        where `readout-replay` sends each kind of readout: detector
                        events to the BIFROST EFU, monitor events to the cbm EFU

The simulation hands no event to an EFU while it runs. Every detected neutron, and every
neutron a beam monitor counts, is recorded by a `Collector*` component into `bifrost.h5`
in the run's output directory, for `readout-replay` to send to the EFUs afterwards -- on
the same machine straight away, or anywhere later. Times are stored as the full
source-to-detector time and folded into the frame they arrive in on replay, as the
detector electronics would report them.

With `--live` it builds the instrument that streams instead: `ReadoutCAEN` sends each
detected neutron to the BIFROST EFU while the simulation runs, and the monitors publish
the histograms McStas makes. Its files are named `bifrost_live.*` rather than `bifrost.*`,
so the two can sit side by side; there is no cbm EFU and no replay, so no `bifrost_cbm.json`
or `bifrost.senders.json`. The README describes which commands run which.

The monitors are read out as ECDC reads the real ones -- cbm1 to cbm5 -- and the cbm EFU
histograms every one of them over 14 pulses. `bifrost.json` describes those histograms,
not the ones McStas writes.

Two instruments are built because they answer different questions. The file-writer wants
the instrument as it is -- motors, choppers, analyzers, detectors -- and has no use for
the Monte Carlo bookkeeping a simulated sample needs, nor for the components that hand
simulated events to the EFU. So `bifrost.json` comes from `instrument()` as niess ships
it, and only the simulation gets the sample and the readout.

`bifrost.json` is named the way ECDC binds the real instrument: the real topics, and the
real PV names prefixed `mcstas:` so a simulated PV is never taken for the real one;
`--real-pv-names` drops that prefix, for where every EPICS server and client is held to
this machine, as mccode-plumber's are inside DITTO and stacktainer. Each
log a parameter fills names it, and mccode-plumber serves, forwards and sets those PVs
before every scan point -- including every instrument parameter's own `mcstas:<name>`,
which is why the instrument carries no EPICS component of its own.
"""
from math import atan, pi, sin
from pathlib import Path
from textwrap import dedent

from mccode_antlr.common import InstrumentParameter
from niess.components import Opaque, Section
from scipp import scalar, vector
from scipp.spatial import rotations_from_rotvecs


def get_registries():
    """Component registries niess does not already know about.

    niess registers the ones its own components come from -- chopper-lib, the detector
    tubes, the Rowland analyzers, ... -- at the versions it was released against, so
    naming them again here could only disagree with it.
    """
    from subprocess import run
    from mccode_antlr.reader import LocalRegistry, GitHubRegistry

    registries = [GitHubRegistry(
        name,
        url=f'https://github.com/mcdotstar/{name}',
        filename='pooch-registry.txt',
        version='main'
    ) for name in ('mccode-mcpl-filter',)]

    r = run(['readout-config', '--show', 'compdir'], check=True, capture_output=True, text=True)
    readout_registry = LocalRegistry(name='readout', root=r.stdout.strip())

    return registries + [readout_registry,]


# Run-time parameters used by the sample, rather than by BIFROST itself
MODE_ENERGY = InstrumentParameter.parse('double mode_energy/"meV"=0')
# ESS source frequency: recorded with collected events; the live readout's pulse rate
PULSE_RATE = 14.0
# Where the live readout sends events, as run-time parameters
EFU_ADDRESS = InstrumentParameter.parse('string efu_address="127.0.0.1"')
EFU_PORT = InstrumentParameter.parse('int efu_port=9000')
# Live only: report detection times as a real detector would, measured from the pulse they
# arrive in. 0 sends the full source-to-detector time instead -- useful for checking frame
# unwrapping downstream, but the EFU rejects times beyond its MaxTOFNS (a key in its JSON
# config) unless that is raised. A replay folds times itself.
FOLD_EVENT_TIME = InstrumentParameter.parse('int fold_event_time=1')
# The collector file, in the run's output directory. restage pairs a primary's collector
# files with each scan point's by name, so the monitors, before the split, and the
# detectors, after it, write the same one.
COLLECTOR_FILE = 'bifrost'
# The fraction of each beam monitor's rays its collector records. A replay sends each
# recorded ray Poisson(weight x counting time) times, and with the monitors' real
# efficiencies (niess's `monitor_efficiencies`) that is a few hundred counts a second,
# against millions of rays a point. Keeping ~100 rays per expected count leaves the
# replayed counts as good as recording every ray. For a 20 s point and `-n 1M-4M+10G`,
# what one point of the a3 scan recorded and would send:
#   monitor        rays/point  rate x efficiency  counts/point  keep
#   psc            12.1M       6.9e7 x 1e-7       140           1e-3
#   overlap        5.2M        4.5e7 x 1e-5       8900          0.2
#   bandwidth      4.2M        3.8e7 x 1e-5       7500          0.2
#   normalization  3.4M        3.1e7 x 1e-5       6300          0.2
#   elastic        ~74k        310 x 0.23         1400          1 (the default; ~50 rays a count)
# A longer counting time keeps fewer rays per count; revisit these well before 10x longer.
MONITOR_KEEP = {'psc_monitor': 1e-3, 'overlap_monitor': 0.2, 'bandwidth_monitor': 0.2,
                'normalization_monitor': 0.2}
# The EFUs' UDP ports, as `mp-nexus-services` is told to start them
DETECTOR_EFU_PORT = 9000
MONITOR_EFU_PORT = 9001
# The analyzers pass `show_construction=showconstruction`, but niess 0.7.0 does not
# declare it, so an instrument emitted from `instrument()` alone does not compile.
SHOW_CONSTRUCTION = InstrumentParameter.parse('int showconstruction=0')


def here():
    """Position and orientation of something sitting at the origin of its frame."""
    return dict(position=vector([0., 0., 0.], unit='m'),
                orientation=rotations_from_rotvecs(vector([0., 0., 0.], unit='deg')))


def opaque(name, mccode_type, parameters=None, **clauses):
    return Opaque(name=name, mccode_type=mccode_type,
                  mccode_parameters=parameters or {}, **here(), **clauses)


class PhononSample(Section):
    """A lead single crystal: one-phonon, incoherent and Bragg scattering.

    Mounted by `instrument()` in the frame ``sample_rotation`` (a3) turns, so nothing
    here is turned by it itself. What *does* depend on it is a direction in the
    sample-table frame that should be fixed in the lab: the focusing target of the
    incoherent scatterers, which follow the detector tank. They aim at
    ``detector_tank_angle - sample_rotation`` (a4 - a3) for that reason.

    The angle is written into the targets themselves, not set in INITIALIZE. restage
    splits the instrument before the sample, and both halves get the instrument's whole
    INITIALIZE: a3 and a4 there make them parameters of the primary spectrometer too, so
    every a3 point of a scan becomes a primary simulation of its own. (INITIALIZE was a
    workaround for mccode-antlr up to 0.31.2, which promoted only one instrument parameter
    per component-parameter expression and emitted the other as an undeclared C name.)

    Each neutron is given to exactly one scattering process, and the one-phonon and
    incoherent processes each focus on one of two targets: the analyzers, or the
    Bragg-peak monitor. Both choices are random, and the neutron's weight is divided by
    the probability of the choice made, so that on average every process and every
    target is counted in full -- the fractions only decide where the statistics go.

    No beryllium filter here: the tank's radial filter-collimator is where BIFROST's
    beryllium is, and a filter at the sample position would scatter the direct beam too.

    Emitted as its own ``%include``d section, which brings the particle variables its
    switching needs with it.
    """
    sample_choice: Opaque
    flat_mode_0: Opaque
    phonon_mode_0: Opaque
    flat_mode_1: Opaque
    phonon_mode_1: Opaque
    single_crystal: Opaque

    def __mccode_enter__(self, visit):
        entered = super().__mccode_enter__(visit)
        assembler = visit.context.assembler
        assembler.user_vars(dedent("""
        int sample_switch;
        int detector_switch;
        double event_time_zero;
        """))
        return entered

    @classmethod
    def build(cls, incoherent=0.02, elastic=0.03):
        """
        incoherent, elastic:
            How often a neutron is given to the incoherent scatterer and to the single
            crystal's Bragg scattering; the rest go to the one-phonon scatterer.

        The focusing scatterers aim at the Bragg-peak monitor in proportion to its share
        of their targets' solid angle, ~7.7 %, and at the analyzers otherwise. Aiming at
        the monitor less only made its rays fewer and heavier: at 1 % of its share it
        recorded ~180 a point, one carrying 30-60 % of the point's counts, for ~4 % more
        rays at the analyzers.
        """
        if not (0 < incoherent and 0 < elastic and incoherent + elastic < 1):
            raise ValueError('need 0 < incoherent, 0 < elastic, incoherent + elastic < 1')
        inelastic = 1 - incoherent - elastic
        # TODO these constants can/should come from niess directly
        tank_angular_width = 90.0  # degrees
        tank_angular_height = 3.0  # degrees
        bragg_peak_angular_width = 2 * atan(25.4 / 800) * 180 / pi
        bragg_peak_angular_height = 2 * atan(1.7 * 25.4 / 800) * 180 / pi
        bragg_peak_offset = 59.  # degrees

        def solid_angle(w_deg, h_deg):
            return 2 * (w_deg * pi / 180) * sin((h_deg * pi / 180) / 2)

        tank_solid_angle = solid_angle(tank_angular_width, tank_angular_height)
        bragg_peak_solid_angle = solid_angle(bragg_peak_angular_width, bragg_peak_angular_height)
        # How often a focused neutron aims at the monitor: its share of the solid angle
        monitor = bragg_peak_solid_angle / (tank_solid_angle + bragg_peak_solid_angle)

        # sample_switch: 2 incoherent, 1 elastic (Bragg), 0 inelastic (one-phonon)
        # detector_switch: 1 Bragg-peak monitor, 0 analyzers
        choice = opaque('sample_choice', 'Arm', extend=dedent(f"""
        double r = rand01();
        sample_switch = (r < {incoherent}) ? 2 : (r < {incoherent + elastic}) ? 1 : 0;
        p /= (sample_switch == 2) ? {incoherent} : (sample_switch == 1) ? {elastic} : {inelastic};
        detector_switch = (rand01() < {monitor}) ? 1 : 0;
        // Single_crystal can not focus, so only the other processes aim at a target
        if (sample_switch != 1) p /= detector_switch ? {monitor} : {1 - monitor};
        """))

        pb_a = 5.05053400
        pb_coh_b = 9.405  # fm, NIST tables
        pb_mass = 207.2  # au, periodic table
        # from ~2 THz at (100) in Brockhouse paper
        pb_group_velocity = (2 * 4.4) / (2 * 3.14159 / pb_a)  # meV Angstrom

        focusing = {}
        for i, (w, h, o) in enumerate((
                (tank_angular_width, tank_angular_height, 0),
                (bragg_peak_angular_width, bragg_peak_angular_height, bragg_peak_offset)
        )):
            # detector_tank_angle turns the tank about the lab; sample_rotation turns this
            # frame. See the class docstring.
            angle = f"detector_tank_angle - sample_rotation + {o}"
            target = {'target_x': f"sin(DEG2RAD * ({angle}))",
                      'target_y': 0.0,
                      'target_z': f"cos(DEG2RAD * ({angle}))"}
            mode = {'radius': 0.005, 'yheight': 0.02, 'thickness': 0.001,
                    'focus_ah': h, 'focus_aw': w, **target,
                    'Etrans': MODE_ENERGY, 'deltaE': 0.0001, 'order': 1}
            focusing[f'flat_mode_{i}'] = opaque(
                f'flat_mode_{i}', 'Incoherent', mode, extend='event_time_zero=t;',
                when=f'sample_switch == 2 && detector_switch == {i}')

            mode = {'radius': 0.005, 'yheight': 0.02,
                    'focus_ah': h, 'focus_aw': w, **target,
                    'sigma_abs': 0, 'sigma_inc': 0,
                    'a': pb_a, 'b': pb_coh_b, 'M': pb_mass, 'c': pb_group_velocity,
                    'DW': 1, 'T': 1}
            focusing[f'phonon_mode_{i}'] = opaque(
                f'phonon_mode_{i}', 'Phonon_simple', mode, extend='event_time_zero=t;',
                when=f'sample_switch == 0 && detector_switch == {i}')

        # The single-crystal component can not focus so it does not get duplicated
        mode = {'radius': 0.005, 'yheight': 0.02, 'mosaic': 5,
                'recip_cell': 0, 'ax': pb_a, 'by': pb_a, 'cz': pb_a,
                'reflections': '"Pb_mp-20483_conventional_standard.lau"'}
        crystal = opaque('single_crystal', 'Single_crystal', mode,
                         extend='event_time_zero=t;', when='sample_switch == 1')

        return cls(sample_choice=choice, single_crystal=crystal, **focusing)


# Which ring, FEN and tube a detected neutron is read out on. RING is (0|1) for cassettes
# 1-3, (2|3) for 4-6 and (4|5) for 7-9.
TRANSLATE_IDS = dedent("""
RING = (int)(rand01() * 2) + 2*(int)((secondary_cassette-1)/3);
TUBE = (secondary_cassette - 1 - 3*(int)((secondary_cassette - 1) / 3)) + 3 * (analyzer - 1);
FEN = 0;
// (do not) shift event-time to be sample-to-detector time (instead of source-to-detector time):
// event_time -= event_time_zero;
""")


class Readout(Section):
    """Record detected neutrons for replay to the EFU.

    Not part of the instrument: these turn a McStas event into what the detector
    electronics would have sent, and store it with its weight. `readout-replay` sends
    them to the EFU after the run. Parameter values reach EPICS without the instrument's
    help: mccode-plumber puts every one on its PV before each scan point.
    """
    translate_ids: Opaque
    readout: Opaque

    def __mccode_enter__(self, visit):
        entered = super().__mccode_enter__(visit)
        assembler = visit.context.assembler
        # The triplets write charges, time and tube into these particle variables by
        # name, but niess 0.7.0 does not declare them.
        assembler.user_vars(dedent("""
        int event_charge_left;
        int event_charge_right;
        double event_time;
        int RING;
        int FEN;
        int TUBE;
        """))
        # A and B as the triplets name them: with A the charge at the wire's start, the
        # EFU's A/(A+B) runs as niess's `efu_calibration` -- from the real instrument --
        # expects.
        assembler.declare(dedent("""
        const char * charge_a_name = "event_charge_left";
        const char * charge_b_name = "event_charge_right";
        const char * detection_time_name = "event_time";
        """))
        return entered

    @classmethod
    def build(cls):
        translate = opaque('translate_ids', 'Arm', when='flag', extend=TRANSLATE_IDS)
        # Which EFU the events go to is replay's business, so none is stored here
        pars = dict(ring='"RING"', fen='"FEN"', tube='"TUBE"',
                    a_name='charge_a_name', b_name='charge_b_name', tof='detection_time_name',
                    pulse_rate=PULSE_RATE, filename=f'"{COLLECTOR_FILE}"')
        readout = opaque('readout', 'CollectorCAEN', pars, when='flag')
        return cls(translate_ids=translate, readout=readout)


class LiveReadout(Readout):
    """Hand detected neutrons to the EFU while the simulation runs.

    `ReadoutCAEN` sends each event as it is detected, against the pulse the wall clock is
    in, so the EFU, the file-writer and everything else must already be running.
    """
    def __mccode_enter__(self, visit):
        entered = super().__mccode_enter__(visit)
        assembler = visit.context.assembler
        # EXTEND code cannot read an instrument parameter by name; a DECLARE'd variable
        # set in INITIALIZE, where it can, is visible everywhere.
        assembler.declare('double event_time_period;')
        assembler.initialize(f'event_time_period = {FOLD_EVENT_TIME.name} ? 1.0 / {PULSE_RATE} : 0.0;')
        return entered

    @classmethod
    def build(cls):
        translate = opaque('translate_ids', 'Arm', when='flag', extend=TRANSLATE_IDS + dedent("""
        // BIFROST's flight path is longer than one ESS frame: report the time since the
        // pulse the neutron arrives in, as the detector electronics would
        if (event_time_period > 0) event_time = fmod(event_time, event_time_period);
        """))
        pars = dict(ring='"RING"', fen='"FEN"', tube='"TUBE"', event_mode='"p"',
                    a_name='charge_a_name', b_name='charge_b_name', tof='detection_time_name',
                    ip=EFU_ADDRESS, port=EFU_PORT, broadcast=1, pulse_rate=PULSE_RATE)
        readout = opaque('readout', 'ReadoutCAEN', pars, when='flag')
        return cls(translate_ids=translate, readout=readout)


def bifrost(**params):
    """BIFROST as niess describes it, with the source this simulation runs."""
    from niess.bifrost.bifrost import instrument
    inst = instrument(**params)
    source = inst.mount_of('primary').content.moderator
    source.n_pulses = 1
    source.accelerator_power = scalar(2.0, unit='MW')
    return inst


def simulated_bifrost(live: bool = False, sample=None):
    """BIFROST with the sample on its table and the readout after the detectors.

    ``live`` streams detected neutrons to the EFU as the simulation runs; otherwise they
    are collected for replay. ``sample`` is any niess section, the lead `PhononSample`
    if not given.
    """
    from msgspec.structs import replace
    from niess.instrument import Mount

    inst = bifrost(sample=PhononSample.build() if sample is None else sample)
    readout = LiveReadout.build() if live else Readout.build()
    return replace(
        inst,
        parts=inst.parts + (Mount(name='readout', content=readout),),
        parameters=inst.parameters + (SHOW_CONSTRUCTION,) + ((FOLD_EVENT_TIME,) if live else ()),
    )


def make_bifrost_instr(inst, live: bool = False):
    from mccode_antlr import Flavor
    from mccode_antlr.assembler import Assembler
    from niess.mccode import to_mccode
    from niess.chopcalc import narrow_source_wavelengths, train_from_instrument
    from niess.cbm import Collect

    assembler = Assembler(inst.name, flavor=Flavor.MCSTAS, registries=get_registries())
    # Collected, each beam monitor is followed by a collector recording the rays it
    # counts, as its cbm EFU channel would read them; live, monitors only histogram
    to_mccode(inst, assembler=assembler,
              collect=None if live else Collect(filename=COLLECTOR_FILE,
                                                keep_probability=MONITOR_KEEP))

    # Restrict the source to the wavelengths the chopper train passes, recomputed at run
    # time from the chopper parameters. The source keeps emitting for about this long
    # after the protons arrive: time to peak brightness + the plateau + some extra.
    #
    # niess's PolygonESSModerator (chopper-lib's Polygon_ESS_butterfly) would draw rays only
    # from the (wavelength, emission-time) region the whole train passes; this call then
    # publishes the train for it too. polygon_moderator_check.py measures whether it is
    # faster and statistically equivalent at the sample before it is used here.
    latest = 2.0e-4 + 2.86e-3 + 2e-3
    narrow_source_wavelengths(
        assembler, chopper_train=train_from_instrument(inst, latest_emission=latest),
        strict=True)

    # Where restage's `splitrun` may split the simulation: here, just after the end of the
    # guide, before the exchangeable mask, and at niess's `split_before_sample`. It takes
    # the later one a scan still shares primaries at -- before the sample for an a3 scan,
    # so the mask, jaws and normalization monitor are simulated once, not once a point --
    # and this one when the mask or jaws are scanned too.
    last_window = 'unit_88_exit_window'
    instr = assembler.instrument
    instr.insert_component('split_at', 'Arm', after=last_window,
                           at_relative=((0, 0, 0.002), last_window),
                           rotate_relative=((0, 0, 0), last_window))
    return instr


def ditto_instrument(sample, collect: bool = False):
    """The build a DITTO digital model counts with, around another sample, for its
    `load_sample`: the live build, or with ``collect`` the collecting one."""
    return make_bifrost_instr(simulated_bifrost(not collect, sample), not collect)


def ditto_model(real_pv_names: bool = False, collect: bool = False):
    """A DITTO digital model's NeXus structure and McStas instrument.

    The live build, as `--live` writes them to `bifrost_live.json` and
    `bifrost_live.instr.json`: detector events stream to the EFU while the simulation
    runs, and the monitors send McStas' histograms. With ``collect``, the build this
    script writes by default: every detector and monitor is collected, a count replays
    the point to the EFUs `ditto_efus` describes, and the monitors publish what the beam
    monitor EFU makes of their events, as the real instrument's do.
    """
    live = not collect
    return (to_nexus_structure(bifrost(), live, real_pv_names),
            make_bifrost_instr(simulated_bifrost(live), live))


#: Where the splitrun image keeps the EFUs, their builds and configurations
EFU_ROOT = Path('/event-formation-unit')


def ditto_efus(directory, address: str = '127.0.0.1') -> dict:
    """The EFUs a collecting DITTO model replays into, and where to send what.

    Writes the detector EFU's calibration, the beam monitor EFU's configuration and
    `readout-replay`'s senders into ``directory``, as this script's own run writes them
    beside the instrument, and returns ``{'efu': [...], 'senders': path}``: the two EFUs
    as `mp-nexus-services --efu` takes them, and the senders file. The beam monitor EFU
    reads no calibration, but the services want a file, so it is given its
    configuration twice, as `services.sh` does.
    """
    import json
    from niess.cbm import efu_config

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    calibration = directory / 'bifrost.efu_calibration.json'
    calibration.write_text(json.dumps(
        simulated_bifrost().mount_of('tank').content.efu_calibration(), indent=2))
    monitors = directory / 'bifrost_cbm.json'
    monitors.write_text(json.dumps(efu_config(bifrost()), indent=2))
    senders = directory / 'bifrost.senders.json'
    senders.write_text(json.dumps(replay_senders(address), indent=2))
    detector_config = EFU_ROOT / 'configs' / 'bifrost' / 'configs' / 'bifrost.json'
    return {
        'efu': [
            f'name:bifrost,binary:{EFU_ROOT}/bin/bifrost,config:{detector_config},'
            f'calibration:{calibration},topic:bifrost_detector,port:{DETECTOR_EFU_PORT}',
            f'name:cbm,binary:{EFU_ROOT}/bin/cbm,config:{monitors},calibration:{monitors},'
            f'topic:bifrost_beam_monitor,port:{MONITOR_EFU_PORT}',
        ],
        'senders': str(senders),
    }


def to_nexus_structure(inst, live: bool = False, real_pv_names: bool = False):
    """The file-writer's structure, named as ECDC binds the real instrument.

    The simulated PVs are the real ones prefixed `mcstas:`, unless `real_pv_names`.
    """
    from niess.bifrost.ecdc import bifrost_streams
    from niess.nexus import to_nexus_structure
    from niess.nexus.bifrost import BIFROST_REGISTRY
    # Collected, the monitors publish what the cbm EFU makes of the replayed rays; live,
    # the histograms McStas makes
    return to_nexus_structure(inst, registry=BIFROST_REGISTRY, streams=bifrost_streams(**({'prefix': ''} if real_pv_names else {})),
                              efu_monitors=not live)


def replay_senders(address: str = '127.0.0.1') -> dict:
    """Where `readout-replay` sends each kind of readout, as its `--config` reads it."""
    def sender(detector, readout, port):
        return {'detector_type': detector, 'readout_type': readout,
                'ip_address': address, 'udp_port': port, 'tcp_port': 0}
    return {'senders': [
        sender('BIFROST', 'CAEN', DETECTOR_EFU_PORT),
        sender('CBM0', 'BM0', MONITOR_EFU_PORT),
        sender('CBMI', 'BMI', MONITOR_EFU_PORT),
    ]}


def main():
    import json
    from argparse import ArgumentParser
    from mccode_antlr.io.json import save_json

    from niess.cbm import efu_config
    try:
        # The replays sample counts by weight, which is only sane with real efficiencies:
        # at 1, the pulse-shaping monitor alone would send ~1e9 counts a point.
        from niess.bifrost.parameters import monitor_efficiencies  # noqa: F401
    except ImportError:
        raise SystemExit('This niess gives the beam monitors no efficiencies; '
                         'install the release after 0.9.1 (see requirements.txt)')

    parser = ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--live', action='store_true',
                        help='Build the instrument that streams to the EFU while it runs, '
                             'as bifrost_live.*, instead of the one that collects for replay')
    parser.add_argument('--real-pv-names', action='store_true',
                        help='Name the simulated PVs exactly as the real ones, without the '
                             'mcstas: prefix. Only where EPICS is held to this machine, '
                             'as in DITTO and stacktainer')
    args = parser.parse_args()
    live = args.live

    vanilla = bifrost()
    # The instrument is BIFROST either way; only the files say which build they are
    stem = f'{vanilla.name}_live' if live else vanilla.name
    with open(f'{stem}.json', 'w') as file:
        json.dump(to_nexus_structure(vanilla, live, args.real_pv_names), file)
    if not live:
        with open(f'{vanilla.name}_cbm.json', 'w') as file:
            json.dump(efu_config(vanilla), file, indent=2)
        with open(f'{vanilla.name}.senders.json', 'w') as file:
            json.dump(replay_senders(), file, indent=2)

    simulated = simulated_bifrost(live)
    # From the simulated tank's own triplets, whose resistances and resistivities are
    # the ones the Detector_tubes components divide charge by
    with open(f'{simulated.name}.efu_calibration.json', 'w') as file:
        json.dump(simulated.mount_of('tank').content.efu_calibration(), file, indent=2)

    instr = make_bifrost_instr(simulated, live)
    save_json(instr, f'{stem}.instr.json')
    # Not a directory named just `bifrost`: mccode-plumber looks for the EFU binary by
    # the instrument's name and takes any existing path of that name before searching PATH.
    instr.to_files(Path(f'{stem}-instr'))


if __name__ == '__main__':
    main()
