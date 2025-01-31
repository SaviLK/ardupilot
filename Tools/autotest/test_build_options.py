#!/usr/bin/env python3

"""
Contains functions used to test the ArduPilot build_options.py structures

To extract feature sizes:

cat >> /tmp/extra-hwdef.dat <<EOF
undef AP_BARO_MS56XX_ENABLED
define AP_BARO_MS56XX_ENABLED 1
EOF

nice time ./Tools/autotest/test_build_options.py --board=CubeOrange --extra-hwdef=/tmp/extra-hwdef.dat --no-run-with-defaults --no-disable-all --no-enable-in-turn | tee /tmp/tbo-out  # noqa
grep 'sabling.*saves' /tmp/tbo-out

 - note that a lot of the time explicitly disabling features will make the binary larger as the ROMFS includes the generated hwdef.h which will have the extra define in it  # noqa

AP_FLAKE8_CLEAN
"""

from __future__ import print_function

import fnmatch
import optparse
import os
import pathlib
import re
import sys

from pysim import util

sys.path.insert(1, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
import extract_features  # noqa


class TestBuildOptionsResult(object):
    '''object to return results from a comparison'''

    def __init__(self, feature, vehicle, bytes_delta):
        self.feature = feature
        self.vehicle = vehicle
        self.bytes_delta = bytes_delta


class TestBuildOptions(object):
    def __init__(self,
                 match_glob=None,
                 do_step_disable_all=True,
                 do_step_disable_none=False,
                 do_step_disable_defaults=True,
                 do_step_disable_in_turn=True,
                 do_step_enable_in_turn=True,
                 build_targets=None,
                 board="CubeOrange",  # DevEBoxH7v2 also works
                 extra_hwdef=None,
                 emit_disable_all_defines=None,
                 resume=False,
                 ):
        self.extra_hwdef = extra_hwdef
        self.sizes_nothing_disabled = None
        self.match_glob = match_glob
        self.do_step_disable_all = do_step_disable_all
        self.do_step_disable_none = do_step_disable_none
        self.do_step_run_with_defaults = do_step_disable_defaults
        self.do_step_disable_in_turn = do_step_disable_in_turn
        self.do_step_enable_in_turn = do_step_enable_in_turn
        self.build_targets = build_targets
        if self.build_targets is None:
            self.build_targets = self.all_targets()
        self._board = board
        self.emit_disable_all_defines = emit_disable_all_defines
        self.resume = resume
        self.results = {}

        self.enable_in_turn_results = {}
        self.sizes_everything_disabled = None

    def must_have_defines_for_board(self, board):
        '''return a set of defines which must always be enabled'''
        must_have_defines = {
            "CubeOrange": frozenset([
                'AP_BARO_MS56XX_ENABLED',
                'AP_COMPASS_LSM303D_ENABLED',
                'AP_COMPASS_AK8963_ENABLED',
                'AP_COMPASS_AK09916_ENABLED',
                'AP_COMPASS_ICM20948_ENABLED',
            ]),
            "CubeBlack": frozenset([
                'AP_BARO_MS56XX_ENABLED',
                'AP_COMPASS_LSM303D_ENABLED',
                'AP_COMPASS_AK8963_ENABLED',
                'AP_COMPASS_AK09916_ENABLED',
                'AP_COMPASS_ICM20948_ENABLED',
            ]),
            "Pixhawk6X-GenericVehicle": frozenset([
                "AP_BARO_BMP388_ENABLED",
                "AP_BARO_ICP201XX_ENABLED",
            ]),
        }
        return must_have_defines.get(board, frozenset([]))

    def must_have_defines(self):
        return self.must_have_defines_for_board(self._board)

    @staticmethod
    def all_targets():
        return ['copter', 'plane', 'rover', 'antennatracker', 'sub', 'blimp']

    def progress(self, message):
        print("###### %s" % message, file=sys.stderr)

    # swiped from app.py:
    def get_build_options_from_ardupilot_tree(self):
        '''return a list of build options'''
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "build_options.py",
            os.path.join(os.path.dirname(os.path.realpath(__file__)),
                         '..', 'scripts', 'build_options.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.BUILD_OPTIONS

    def write_defines_to_file(self, defines, filepath):
        self.write_defines_to_Path(defines, pathlib.Path(filepath))

    def write_defines_to_Path(self, defines, Path):
        lines = []
        lines.extend(["undef %s\n" % (a, ) for (a, b) in defines.items()])
        lines.extend(["define %s %s\n" % (a, b) for (a, b) in defines.items()])
        content = "".join(lines)
        Path.write_text(content)

    def get_disable_defines(self, feature, options):
        '''returns a hash of (name, value) defines to turn feature off -
        recursively gets dependencies'''
        ret = {
            feature.define: 0,
        }
        added_one = True
        while added_one:
            added_one = False
            for option in options:
                if option.define in ret:
                    continue
                if option.dependency is None:
                    continue
                for dep in option.dependency.split(','):
                    f = self.get_option_by_label(dep, options)
                    if f.define not in ret:
                        continue

                    # print("%s requires %s" % (option.define, f.define), file=sys.stderr)
                    added_one = True
                    ret[option.define] = 0
                    break
        return ret

    def update_get_enable_defines_for_feature(self, ret, feature, options):
        '''recursive function to turn on required feature and what it depends
        on'''
        ret[feature.define] = 1
        if feature.dependency is None:
            return
        for depname in feature.dependency.split(','):
            dep = None
            for f in options:
                if f.label == depname:
                    dep = f
            if dep is None:
                raise ValueError("Invalid dep (%s) for feature (%s)" %
                                 (depname, feature.label))
            self.update_get_enable_defines_for_feature(ret, dep, options)

    def get_enable_defines(self, feature, options):
        '''returns a hash of (name, value) defines to turn all features *but* feature (and whatever it depends on) on'''
        ret = self.get_disable_all_defines()
        self.update_get_enable_defines_for_feature(ret, feature, options)
        for define in self.must_have_defines_for_board(self._board):
            ret[define] = 1
        return ret

    def test_disable_feature(self, feature, options):
        defines = self.get_disable_defines(feature, options)

        if len(defines.keys()) > 1:
            self.progress("Disabling %s disables (%s)" % (
                feature.define,
                ",".join(defines.keys())))

        self.test_compile_with_defines(defines)

        # if the feature is truly disabled then extract_features.py
        # should say so:
        for target in self.build_targets:
            path = self.target_to_elf_path(target)
            extracter = extract_features.ExtractFeatures(path)
            (compiled_in_feature_defines, not_compiled_in_feature_defines) = extracter.extract()
            for define in defines:
                # the following defines are known not to work on some
                # or all vehicles:
                feature_define_whitelist = set([
                    'AP_RANGEFINDER_ENABLED',  # only at vehicle level ATM
                    'HAL_PERIPH_SUPPORT_LONG_CAN_PRINTF',  # no symbol
                ])
                if define in compiled_in_feature_defines:
                    error = f"feature gated by {define} still compiled into ({target}); extract_features.py bug?"
                    if define in feature_define_whitelist:
                        print("warn: " + error)
                    else:
                        raise ValueError(error)

    def test_enable_feature(self, feature, options):
        defines = self.get_enable_defines(feature, options)

        enabled = list(filter(lambda x : bool(defines[x]), defines.keys()))

        if len(enabled) > 1:
            self.progress("Enabling %s enables (%s)" % (
                feature.define,
                ",".join(enabled)))

        self.test_compile_with_defines(defines)

    def board(self):
        '''returns board to build for'''
        return self._board

    def test_compile_with_defines(self, defines):
        extra_hwdef_filepath = "/tmp/extra.hwdef"
        self.write_defines_to_file(defines, extra_hwdef_filepath)
        if self.extra_hwdef is not None:
            content = open(self.extra_hwdef, "r").read()
            with open(extra_hwdef_filepath, "a") as f:
                f.write(content)
        util.waf_configure(
            self.board(),
            extra_hwdef=extra_hwdef_filepath,
        )
        for t in self.build_targets:
            try:
                util.run_cmd([util.relwaf(), t])
            except Exception:
                print("Failed to build (%s) with things disabled" %
                      (t,))
                raise

    def target_to_path(self, target, extension=None):
        '''given a build target (e.g. copter), return expected path to .bin
        file for that target'''
        target_to_binpath = {
            "copter": "arducopter",
            "plane": "arduplane",
            "rover": "ardurover",
            "antennatracker": "antennatracker",
            "sub": "ardusub",
            "blimp": "blimp",
        }
        filename = target_to_binpath[target]
        if extension is not None:
            filename += "." + extension
        return os.path.join("build", self.board(), "bin", filename)

    def target_to_bin_path(self, target):
        '''given a build target (e.g. copter), return expected path to .bin
        file for that target'''
        return self.target_to_path(target, 'bin')

    def target_to_elf_path(self, target):
        '''given a build target (e.g. copter), return expected path to .elf
        file for that target'''
        return self.target_to_path(target)

    def find_build_sizes(self):
        '''returns a hash with size of all build targets'''
        ret = {}
        for target in self.build_targets:
            path = self.target_to_bin_path(target)
            ret[target] = os.path.getsize(path)
        return ret

    def csv_for_results(self, results):
        '''return a string with csv for results'''
        features = sorted(results.keys())
        all_vehicles = set()
        for feature in features:
            all_vehicles.update(list(results[feature].keys()))
        sorted_all_vehicles = sorted(list(all_vehicles))
        ret = ""
        ret += ",".join(["Feature"] + sorted_all_vehicles) + "\n"
        for feature in features:
            line = [feature]
            feature_results = results[feature]
            for vehicle in sorted_all_vehicles:
                bytes_delta = ""
                if vehicle in feature_results:
                    result = feature_results[vehicle]
                    bytes_delta = result.bytes_delta
                line.append(str(bytes_delta))
            ret += ",".join(line) + "\n"
        return ret

    def disable_in_turn_check_sizes(self, feature, sizes_nothing_disabled):
        if not self.do_step_disable_none:
            self.progress("disable-none skipped, size comparison not available")
            return
        current_sizes = self.find_build_sizes()
        for (build, new_size) in current_sizes.items():
            old_size = sizes_nothing_disabled[build]
            self.progress("Disabling %s(%s) on %s saves %u bytes" %
                          (feature.label, feature.define, build, old_size - new_size))
            if feature.define not in self.results:
                self.results[feature.define] = {}
            self.results[feature.define][build] = TestBuildOptionsResult(feature.define, build, old_size - new_size)
            with open("/tmp/some.csv", "w") as f:
                f.write(self.csv_for_results(self.results))

    def run_disable_in_turn(self):
        progress_file = pathlib.Path("/tmp/run-disable-in-turn-progress")
        resume_number = self.resume_number_from_progress_Path(progress_file)
        options = self.get_build_options_from_ardupilot_tree()
        count = 1
        for feature in sorted(options, key=lambda x : x.define):
            if resume_number is not None:
                if count < resume_number:
                    count += 1
                    continue
            if self.match_glob is not None:
                if not fnmatch.fnmatch(feature.define, self.match_glob):
                    continue
            with open(progress_file, "w") as f:
                f.write(f"{count}/{len(options)} {feature.define}\n")
                #            if feature.define < "WINCH_ENABLED":
                #                count += 1
                #                continue
            if feature.define in self.must_have_defines_for_board(self._board):
                self.progress("Feature %s(%s) (%u/%u) is a MUST-HAVE" %
                              (feature.label, feature.define, count, len(options)))
                count += 1
                continue
            self.progress("Disabling feature %s(%s) (%u/%u)" %
                          (feature.label, feature.define, count, len(options)))
            self.test_disable_feature(feature, options)
            count += 1
            self.disable_in_turn_check_sizes(feature, self.sizes_nothing_disabled)

    def enable_in_turn_check_sizes(self, feature, sizes_everything_disabled):
        if not self.do_step_disable_all:
            self.progress("disable-none skipped, size comparison not available")
            return
        current_sizes = self.find_build_sizes()
        for (build, new_size) in current_sizes.items():
            old_size = sizes_everything_disabled[build]
            self.progress("Enabling %s(%s) on %s costs %u bytes" %
                          (feature.label, feature.define, build, old_size - new_size))
            if feature.define not in self.enable_in_turn_results:
                self.enable_in_turn_results[feature.define] = {}
            self.enable_in_turn_results[feature.define][build] = TestBuildOptionsResult(feature.define, build, old_size - new_size)  # noqa
            with open("/tmp/enable-in-turn.csv", "w") as f:
                f.write(self.csv_for_results(self.enable_in_turn_results))

    def resume_number_from_progress_Path(self, progress_file):
        if not self.resume:
            return None
        try:
            content = progress_file.read_text().rstrip()
            m = re.match(r"(\d+)/\d+ \w+", content)
            if m is None:
                raise ValueError(f"{progress_file} not matched")
            return int(m.group(1))
        except FileNotFoundError:
            pass
        return None

    def run_enable_in_turn(self):
        progress_file = pathlib.Path("/tmp/run-enable-in-turn-progress")
        resume_number = self.resume_number_from_progress_Path(progress_file)
        options = self.get_build_options_from_ardupilot_tree()
        count = 1
        for feature in options:
            if resume_number is not None:
                if count < resume_number:
                    count += 1
                    continue
            if self.match_glob is not None:
                if not fnmatch.fnmatch(feature.define, self.match_glob):
                    continue
            self.progress("Enabling feature %s(%s) (%u/%u)" %
                          (feature.label, feature.define, count, len(options)))
            with open(progress_file, "w") as f:
                f.write(f"{count}/{len(options)} {feature.define}\n")
            self.test_enable_feature(feature, options)
            count += 1
            self.enable_in_turn_check_sizes(feature, self.sizes_everything_disabled)

    def get_option_by_label(self, label, options):
        for x in options:
            if x.label == label:
                return x
        raise ValueError("No such option (%s)" % label)

    def get_disable_all_defines(self):
        '''returns a hash of defines which turns all features off'''
        options = self.get_build_options_from_ardupilot_tree()
        defines = {}
        for feature in options:
            if self.match_glob is not None:
                if not fnmatch.fnmatch(feature.define, self.match_glob):
                    continue
            defines[feature.define] = 0
        for define in self.must_have_defines_for_board(self._board):
            defines[define] = 1

        return defines

    def run_disable_all(self):
        defines = self.get_disable_all_defines()
        self.test_compile_with_defines(defines)
        self.sizes_everything_disabled = self.find_build_sizes()

    def run_disable_none(self):
        self.test_compile_with_defines({})
        self.sizes_nothing_disabled = self.find_build_sizes()

    def run_with_defaults(self):
        options = self.get_build_options_from_ardupilot_tree()
        defines = {}
        for feature in options:
            defines[feature.define] = feature.default
        self.test_compile_with_defines(defines)

    def check_deps_consistency(self):
        # self.progress("Checking deps consistency")
        options = self.get_build_options_from_ardupilot_tree()
        for feature in options:
            self.get_disable_defines(feature, options)

    def check_duplicate_labels(self):
        '''check that we do not have multiple features with same labels'''
        options = self.get_build_options_from_ardupilot_tree()
        seen_labels = {}
        for feature in options:
            if seen_labels.get(feature.label, None) is not None:
                raise ValueError("Duplicate entries found for label '%s'" % feature.label)
            seen_labels[feature.label] = True

    def do_emit_disable_all_defines(self):
        defines = tbo.get_disable_all_defines()
        for f in self.must_have_defines():
            defines[f] = 1
        tbo.write_defines_to_Path(defines, pathlib.Path("/dev/stdout"))
        sys.exit(0)

    def run(self):
        self.check_deps_consistency()
        self.check_duplicate_labels()

        if self.emit_disable_all_defines:
            self.do_emit_disable_all_defines()
            sys.exit(0)

        if self.do_step_run_with_defaults:
            self.progress("Running run-with-defaults step")
            self.run_with_defaults()
        if self.do_step_disable_all:
            self.progress("Running disable-all step")
            self.run_disable_all()
        if self.do_step_disable_none:
            self.progress("Running disable-none step")
            self.run_disable_none()
        if self.do_step_disable_in_turn:
            self.progress("Running disable-in-turn step")
            self.run_disable_in_turn()
        if self.do_step_enable_in_turn:
            self.progress("Running enable-in-turn step")
            self.run_enable_in_turn()


if __name__ == '__main__':

    parser = optparse.OptionParser()
    parser.add_option("--define-match-glob",
                      type='string',
                      default=None,
                      help='feature define must match this glob to be tested')
    parser.add_option("--no-run-with-defaults",
                      action='store_true',
                      help='Do not run the run-with-defaults step')
    parser.add_option("--no-disable-all",
                      action='store_true',
                      help='Do not run the disable-all step')
    parser.add_option("--no-disable-none",
                      action='store_true',
                      help='Do not run the disable-none step')
    parser.add_option("--no-disable-in-turn",
                      action='store_true',
                      help='Do not run the disable-in-turn step')
    parser.add_option("--no-enable-in-turn",
                      action='store_true',
                      help='Do not run the enable-in-turn step')
    parser.add_option("--build-targets",
                      type='choice',
                      choices=TestBuildOptions.all_targets(),
                      action='append',
                      help='vehicle targets to build')
    parser.add_option("--extra-hwdef",
                      type='string',
                      default=None,
                      help="file containing extra hwdef information")
    parser.add_option("--board",
                      type='string',
                      default="CubeOrange",
                      help='board to build for')
    parser.add_option("--emit-disable-all-defines",
                      action='store_true',
                      help='emit defines used for disabling all features then exit')
    parser.add_option("--resume",
                      action='store_true',
                      help='resume from previous progress file')

    opts, args = parser.parse_args()

    tbo = TestBuildOptions(
        match_glob=opts.define_match_glob,
        do_step_disable_all=not opts.no_disable_all,
        do_step_disable_none=not opts.no_disable_none,
        do_step_disable_defaults=not opts.no_run_with_defaults,
        do_step_disable_in_turn=not opts.no_disable_in_turn,
        do_step_enable_in_turn=not opts.no_enable_in_turn,
        build_targets=opts.build_targets,
        board=opts.board,
        extra_hwdef=opts.extra_hwdef,
        emit_disable_all_defines=opts.emit_disable_all_defines,
        resume=opts.resume,
    )

    tbo.run()
