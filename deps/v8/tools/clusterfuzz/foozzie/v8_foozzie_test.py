#!/usr/bin/env python3
# Copyright 2016 the V8 project authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import os
import random
import re
import subprocess
import sys
import unittest
import unittest.mock

import v8_commands
import v8_foozzie
import v8_fuzz_config
import v8_suppressions

try:
  basestring
except NameError:
  basestring = str

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOOZZIE = os.path.join(BASE_DIR, 'v8_foozzie.py')
TEST_DATA = os.path.join(BASE_DIR, 'testdata')

KNOWN_BUILDS = [
  'd8',
  'clang_x86/d8',
  'clang_x86_v8_arm/d8',
  'clang_x64_v8_arm64/d8',
  'clang_x64_pointer_compression/d8',
]


class ConfigTest(unittest.TestCase):
  def testExperiments(self):
    """Test integrity of probabilities and configs."""
    CONFIGS = v8_foozzie.CONFIGS
    EXPERIMENTS = v8_fuzz_config.FOOZZIE_EXPERIMENTS
    FLAGS = v8_fuzz_config.ADDITIONAL_FLAGS
    # Probabilities add up to 100%.
    first_is_int = lambda x: type(x[0]) == int
    assert all(map(first_is_int, EXPERIMENTS))
    assert sum(x[0] for x in EXPERIMENTS) == 100
    # Configs used in experiments are defined.
    assert all(map(lambda x: x[1] in CONFIGS, EXPERIMENTS))
    assert all(map(lambda x: x[2] in CONFIGS, EXPERIMENTS))
    # The last config item points to a known build configuration.
    assert all(map(lambda x: x[3] in KNOWN_BUILDS, EXPERIMENTS))
    # All flags have a probability.
    first_is_float = lambda x: type(x[0]) == float
    assert all(map(first_is_float, FLAGS))
    first_between_0_and_1 = lambda x: x[0] > 0 and x[0] < 1
    assert all(map(first_between_0_and_1, FLAGS))
    # Test consistent flags.
    second_is_string = lambda x: isinstance(x[1], basestring)
    assert all(map(second_is_string, FLAGS))
    # We allow spaces to separate more flags. We don't allow spaces in the flag
    # value.
    is_flag = lambda x: x.startswith('--')
    all_parts_are_flags = lambda x: all(map(is_flag, x[1].split()))
    assert all(map(all_parts_are_flags, FLAGS))

  def testConfig(self):
    """Smoke test how to choose experiments."""
    config = v8_fuzz_config.Config('foo', random.Random(42))
    experiments = [
      [25, 'ignition', 'jitless', 'd8'],
      [75, 'ignition', 'ignition', 'clang_x86/d8'],
    ]
    flags = [
      [0.1, '--flag'],
      [0.3, '--baz'],
      [0.3, '--foo --bar'],
    ]
    self.assertEqual(
        [
          '--first-config=ignition',
          '--second-config=jitless',
          '--second-d8=d8',
          '--second-config-extra-flags=--baz',
          '--second-config-extra-flags=--foo',
          '--second-config-extra-flags=--bar',
        ],
        config.choose_foozzie_flags(experiments, flags),
    )
    self.assertEqual(
        [
          '--first-config=ignition',
          '--second-config=jitless',
          '--second-d8=d8',
        ],
        config.choose_foozzie_flags(experiments, flags),
    )


class UnitTest(unittest.TestCase):
  def testCluster(self):
    crash_test_example_path = 'CrashTests/path/to/file.js'
    self.assertEqual(
        v8_foozzie.ORIGINAL_SOURCE_DEFAULT,
        v8_foozzie.cluster_failures(''))
    self.assertEqual(
        v8_foozzie.ORIGINAL_SOURCE_CRASHTESTS,
        v8_foozzie.cluster_failures(crash_test_example_path))
    self.assertEqual(
        '_o_O_',
        v8_foozzie.cluster_failures(
            crash_test_example_path,
            known_failures={crash_test_example_path: '_o_O_'}))
    self.assertEqual(
        '98',
        v8_foozzie.cluster_failures('v8/test/mjsunit/apply.js'))

  def testDiff(self):
    def diff_fun(one, two, skip=False):
      suppress = v8_suppressions.get_suppression(skip)
      return suppress.diff_lines(one.splitlines(), two.splitlines())

    one = ''
    two = ''
    diff = None, None
    self.assertEqual(diff, diff_fun(one, two))

    one = 'a \n  b\nc();'
    two = 'a \n  b\nc();'
    diff = None, None
    self.assertEqual(diff, diff_fun(one, two))

    # Ignore line before caret and caret position.
    one = """
undefined
weird stuff
      ^
somefile.js: TypeError: suppressed message
  undefined
"""
    two = """
undefined
other weird stuff
            ^
somefile.js: TypeError: suppressed message
  undefined
"""
    diff = None, None
    self.assertEqual(diff, diff_fun(one, two))

    one = """
Still equal
Extra line
"""
    two = """
Still equal
"""
    diff = '- Extra line', None
    self.assertEqual(diff, diff_fun(one, two))

    one = """
Still equal
"""
    two = """
Still equal
Extra line
"""
    diff = '+ Extra line', None
    self.assertEqual(diff, diff_fun(one, two))

    one = """
undefined
somefile.js: TypeError: undefined is not a constructor
"""
    two = """
undefined
otherfile.js: TypeError: undefined is not a constructor
"""
    diff = """- somefile.js: TypeError: undefined is not a constructor
+ otherfile.js: TypeError: undefined is not a constructor""", None
    self.assertEqual(diff, diff_fun(one, two))

    # Test that skipping suppressions works.
    one = """
v8-foozzie source: foo
weird stuff
      ^
"""
    two = """
v8-foozzie source: foo
other weird stuff
            ^
"""
    self.assertEqual((None, 'foo'), diff_fun(one, two))
    diff = ('-       ^\n+             ^', 'foo')
    self.assertEqual(diff, diff_fun(one, two, skip=True))

  def testOutputCapping(self):
    def output(stdout, is_crash):
      exit_code = -1 if is_crash else 0
      return v8_commands.Output(
          exit_code=exit_code, stdout_bytes=stdout.encode('utf-8'), pid=0)

    def check(stdout1, stdout2, is_crash1, is_crash2, capped_lines1,
              capped_lines2):
      output1 = output(stdout1, is_crash1)
      output2 = output(stdout2, is_crash2)
      self.assertEqual(
          (capped_lines1.encode('utf-8'), capped_lines2.encode('utf-8')),
          v8_suppressions.get_output_capped(output1, output2))

    # No capping, already equal.
    check('1\n2', '1\n2', True, True, '1\n2', '1\n2')
    # No crash, no capping.
    check('1\n2', '1\n2\n3', False, False, '1\n2', '1\n2\n3')
    check('1\n2\n3', '1\n2', False, False, '1\n2\n3', '1\n2')
    # Cap smallest if all runs crash.
    check('1\n2', '1\n2\n3', True, True, '1\n2', '1\n2')
    check('1\n2\n3', '1\n2', True, True, '1\n2', '1\n2')
    check('1\n2', '1\n23', True, True, '1\n2', '1\n2')
    check('1\n23', '1\n2', True, True, '1\n2', '1\n2')
    # Cap the non-crashy run.
    check('1\n2\n3', '1\n2', False, True, '1\n2', '1\n2')
    check('1\n2', '1\n2\n3', True, False, '1\n2', '1\n2')
    check('1\n23', '1\n2', False, True, '1\n2', '1\n2')
    check('1\n2', '1\n23', True, False, '1\n2', '1\n2')
    # The crashy run has more output.
    check('1\n2\n3', '1\n2', True, False, '1\n2\n3', '1\n2')
    check('1\n2', '1\n2\n3', False, True, '1\n2', '1\n2\n3')
    check('1\n23', '1\n2', True, False, '1\n23', '1\n2')
    check('1\n2', '1\n23', False, True, '1\n2', '1\n23')
    # Keep output difference when capping.
    check('1\n2', '3\n4\n5', True, True, '1\n2', '3\n4')
    check('1\n2\n3', '4\n5', True, True, '1\n2', '4\n5')
    check('12', '345', True, True, '12', '34')
    check('123', '45', True, True, '12', '45')

  @unittest.mock.patch('v8_foozzie.DISALLOWED_FLAGS', ['A'])
  @unittest.mock.patch('v8_foozzie.CONTRADICTORY_FLAGS',
                       [('B', 'C'), ('B', 'D')])
  def testFilterFlags(self):
    def check(input_flags, expected):
      self.assertEqual(expected, v8_foozzie.filter_flags(input_flags))

    check([], [])
    check(['A'], [])
    check(['D', 'A'], ['D'])
    check(['A', 'D'], ['D'])
    check(['C', 'D'], ['C', 'D'])
    check(['E', 'C', 'D', 'F'], ['E', 'C', 'D', 'F'])
    check(['B', 'D'], ['D'])
    check(['D', 'B'], ['B'])
    check(['C', 'B', 'D'], ['C', 'D'])
    check(['E', 'C', 'A', 'F', 'B', 'G', 'D'], ['E', 'C', 'F', 'G', 'D'])

  def _test_content(self, filename):
    with open(os.path.join(TEST_DATA, filename)) as f:
      return f.read()

  def _create_execution_configs(self, *extra_flags):
    """Create three execution configs as in production with a fake config
    called `special`.

    The build is cross-arch (build3 is x86) so that the configs have fallbacks
    on the same architecture.
    """
    argv = create_test_cmd_line('build3', 'special', 'fuzz-123.js',
                                *extra_flags)
    options = v8_foozzie.parse_args(argv[2:])
    return v8_foozzie.create_execution_configs(options)

  @unittest.mock.patch('v8_suppressions.DROP_FLAGS_ON_CONTENT',
                       [('--bat', r'\%DontUseThat\(|\%DontUseThis\(')])
  @unittest.mock.patch(
      'v8_foozzie.CONFIGS', {
          'ignition': ['--foo', '--baz'],
          'default': ['--bar', '--baz'],
          'special': ['--bat'],
      })
  def testAdjustConfigs_Matches1(self):
    suppress = v8_suppressions.get_suppression()
    content = self._test_content('fuzz-123.js')
    configs = self._create_execution_configs()
    logs = suppress.adjust_configs(configs, content)
    self.assertEqual(
        ['Dropped second config using --bat based on content rule.'], logs)
    self.assertEqual(2, len(configs))
    self.assertEqual(['--foo', '--baz'], configs[0].config_flags)
    self.assertEqual(['--bar', '--baz'], configs[1].config_flags)

    configs = self._create_execution_configs('--first-config-extra-flags=--bat')
    logs = suppress.adjust_configs(configs, content)
    expected_logs = [
        'Dropped --bat from first config based on content rule.',
        'Dropped second config using --bat based on content rule.',
    ]
    self.assertEqual(expected_logs, logs)
    self.assertEqual(2, len(configs))
    self.assertEqual(['--foo', '--baz'], configs[0].config_flags)
    self.assertEqual(['--bar', '--baz'], configs[1].config_flags)

  @unittest.mock.patch('v8_suppressions.DROP_FLAGS_ON_CONTENT',
                       [('--baz', r'\%DontUseThat\(|\%DontUseThis\(')])
  @unittest.mock.patch(
      'v8_foozzie.CONFIGS', {
          'ignition': ['--foo', '--baz'],
          'default': ['--bar', '--baz'],
          'special': ['--bat'],
      })
  def testAdjustConfigs_Matches2(self):
    suppress = v8_suppressions.get_suppression()
    content = self._test_content('fuzz-123.js')
    configs = self._create_execution_configs()
    logs = suppress.adjust_configs(configs, content)
    expected_logs = [
        'Dropped --baz from first config based on content rule.',
        'Dropped --baz from default config based on content rule.',
    ]
    self.assertEqual(expected_logs, logs)
    self.assertEqual(3, len(configs))
    self.assertEqual(['--foo'], configs[0].config_flags)
    self.assertEqual(['--bar'], configs[1].config_flags)
    self.assertEqual(['--bar'], configs[1].fallback.config_flags)
    self.assertEqual(['--bat'], configs[2].config_flags)
    self.assertEqual(['--bat'], configs[2].fallback.config_flags)

    configs = self._create_execution_configs(
        '--second-config-extra-flags=--baz')
    logs = suppress.adjust_configs(configs, content)
    expected_logs = [
        'Dropped --baz from first config based on content rule.',
        'Dropped --baz from default config based on content rule.',
        'Dropped second config using --baz based on content rule.',
    ]
    self.assertEqual(expected_logs, logs)
    self.assertEqual(2, len(configs))
    self.assertEqual(['--foo'], configs[0].config_flags)
    self.assertEqual(['--bar'], configs[1].config_flags)
    self.assertEqual(['--bar'], configs[1].fallback.config_flags)

  @unittest.mock.patch('v8_suppressions.DROP_FLAGS_ON_CONTENT',
                       [('--baz', r'\%UnusedFun\(')])
  @unittest.mock.patch('v8_foozzie.CONFIGS', {
      'ignition': ['--foo', '--baz'],
      'default': ['--bar'],
      'special': ['--bat'],
  })
  def testAdjustConfigs_DoesntMatch(self):
    suppress = v8_suppressions.get_suppression()
    content = self._test_content('fuzz-123.js')
    configs = self._create_execution_configs(
        '--second-config-extra-flags=--baz')
    logs = suppress.adjust_configs(configs, content)
    self.assertEqual([], logs)
    self.assertEqual(3, len(configs))
    self.assertEqual(['--foo', '--baz'], configs[0].config_flags)
    self.assertEqual(['--bar', '--baz'], configs[1].config_flags)
    self.assertEqual(['--bar', '--baz'], configs[1].fallback.config_flags)
    self.assertEqual(['--bat', '--baz'], configs[2].config_flags)
    self.assertEqual(['--bat', '--baz'], configs[2].fallback.config_flags)


def cut_verbose_output(stdout, n_comp):
  # This removes the first lines containing d8 commands of `n_comp` comparison
  # runs.
  return '\n'.join(stdout.split('\n')[n_comp * 2:])


def create_test_cmd_line(second_d8_dir, second_config, filename, *extra_flags):
  return [
      sys.executable,
      FOOZZIE,
      '--random-seed',
      '12345',
      '--first-d8',
      os.path.join(TEST_DATA, 'baseline', 'd8.py'),
      '--second-d8',
      os.path.join(TEST_DATA, second_d8_dir, 'd8.py'),
      '--first-config',
      'ignition',
      '--second-config',
      second_config,
      os.path.join(TEST_DATA, filename),
  ] + list(extra_flags)


def run_foozzie(second_d8_dir, *extra_flags, **kwargs):
  filename = kwargs.pop('filename', 'fuzz-123.js')
  second_config = kwargs.pop('second_config', 'ignition_turbo')
  cmd = create_test_cmd_line(second_d8_dir, second_config, filename,
                             *extra_flags)
  return subprocess.check_output(cmd, text=True, **kwargs)


class SystemTest(unittest.TestCase):
  """This tests the whole correctness-fuzzing harness with fake build
  artifacts.

  Overview of fakes:
    baseline: Example foozzie output including a syntax error.
    build1: Difference to baseline is a stack trace difference expected to
            be suppressed.
    build2: Difference to baseline is a non-suppressed output difference
            causing the script to fail.
    build3: As build1 but with an architecture difference as well.
  """

  def assert_expected(self, file_name, expected):
    if os.environ.get('GENERATE'):
      with open(os.path.join(TEST_DATA, file_name), 'w') as f:
        f.write(expected)
    with open(os.path.join(TEST_DATA, file_name)) as f:
      self.assertEqual(f.read(), expected)

  def testSyntaxErrorDiffPass(self):
    stdout = run_foozzie('build1', '--skip-smoke-tests')
    self.assertEqual('# V8 correctness - pass\n',
                     cut_verbose_output(stdout, 3))
    # Default comparison includes suppressions.
    self.assertIn('v8_suppressions.js', stdout)
    # Default comparison doesn't include any specific mock files.
    self.assertNotIn('v8_mock_archs.js', stdout)
    self.assertNotIn('v8_mock_webassembly.js', stdout)

  def _testDifferentOutputFail(self, expected_path, *args):
    with self.assertRaises(subprocess.CalledProcessError) as ctx:
      run_foozzie('build2', '--skip-smoke-tests',
                  '--first-config-extra-flags=--flag1',
                  '--first-config-extra-flags=--flag2=0',
                  '--second-config-extra-flags=--flag3', *args)
    e = ctx.exception
    self.assertEqual(v8_foozzie.RETURN_FAIL, e.returncode)
    self.assert_expected(expected_path, cut_verbose_output(e.output, 2))

  def testDifferentOutputFail(self):
    self._testDifferentOutputFail('failure_output.txt')

  def testSmokeTest(self):
    with self.assertRaises(subprocess.CalledProcessError) as ctx:
      run_foozzie('build2')
    e = ctx.exception
    self.assertEqual(v8_foozzie.RETURN_FAIL, e.returncode)
    self.assert_expected('smoke_test_output.txt', e.output)

  def testDifferentArch(self):
    """Test that the architecture-specific mocks are passed to both runs when
    we use executables with different architectures.
    """
    # Build 3 simulates x86, while the baseline is x64.
    stdout = run_foozzie('build3', '--skip-smoke-tests')
    lines = stdout.split('\n')
    # TODO(machenbach): Don't depend on the command-lines being printed in
    # particular lines.
    self.assertIn('v8_mock_archs.js', lines[1])
    self.assertIn('v8_mock_archs.js', lines[3])

  def testDifferentArchFailFirst(self):
    """Test that we re-test against x64. This tests the path that also fails
    on x64 and then reports the error as x64.
    """
    # Build 3 simulates x86 and produces a difference on --bad-flag, but
    # the baseline build shows the same difference when --bad-flag is passed.
    with self.assertRaises(subprocess.CalledProcessError) as ctx:
      run_foozzie('build3', '--skip-smoke-tests',
                  '--second-config-extra-flags=--bad-flag')
    e = ctx.exception
    self.assertEqual(v8_foozzie.RETURN_FAIL, e.returncode)
    self.assert_expected(
        'failure_output_arch.txt', cut_verbose_output(e.output, 3))

  def testDifferentArchFailSecond(self):
    """As above, but we test the path that only fails in the second (ia32)
    run and not with x64 and then reports the error as ia32.
    """
    # Build 3 simulates x86 and produces a difference on --very-bad-flag,
    # which the baseline build doesn't.
    with self.assertRaises(subprocess.CalledProcessError) as ctx:
      run_foozzie('build3', '--skip-smoke-tests',
                  '--second-config-extra-flags=--very-bad-flag')
    e = ctx.exception
    self.assertEqual(v8_foozzie.RETURN_FAIL, e.returncode)
    self.assert_expected(
        'failure_output_second.txt', cut_verbose_output(e.output, 3))

  def testJitless(self):
    """Test that webassembly is mocked out when comparing with jitless."""
    stdout = run_foozzie(
        'build1', '--skip-smoke-tests', second_config='jitless')
    lines = stdout.split('\n')
    # TODO(machenbach): Don't depend on the command-lines being printed in
    # particular lines.
    self.assertIn('v8_mock_webassembly.js', lines[1])
    self.assertIn('v8_mock_webassembly.js', lines[3])

  def testJitlessAndWasmStruct_FlagPassed(self):
    """We keep passing the --jitless flag when no content rule matches.

    The flag passed to one run of build3 causes an output difference.
    """
    with self.assertRaises(subprocess.CalledProcessError) as ctx:
      run_foozzie('build3', '--skip-smoke-tests', second_config='jitless')
    self.assertIn('jitless flag passed', ctx.exception.stdout)
    self.assertNotIn('Adjusted flags and experiments based on the test case',
                     ctx.exception.stdout)

  def testJitlessAndWasmStruct_FlagDropped(self):
    """We drop the --jitless flag when the content rule matches."""
    stdout = run_foozzie(
        'build3',
        '--skip-smoke-tests',
        second_config='jitless',
        filename='fuzz-wasm-struct-123.js')
    self.assertIn('Adjusted flags and experiments based on the test case',
                  stdout)
    self.assertIn(
        'Dropped second config using --jitless based on content rule.', stdout)

  def testSkipSuppressions(self):
    """Test that the suppressions file is not passed when skipping
    suppressions.
    """
    # Compare baseline with baseline. This passes as there is no difference.
    stdout = run_foozzie(
        'baseline', '--skip-smoke-tests', '--skip-suppressions')
    self.assertNotIn('v8_suppressions.js', stdout)

    # Compare with a build that usually suppresses a difference. Now we fail
    # since we skip suppressions.
    with self.assertRaises(subprocess.CalledProcessError) as ctx:
      run_foozzie(
          'build1', '--skip-smoke-tests', '--skip-suppressions')
    e = ctx.exception
    self.assertEqual(v8_foozzie.RETURN_FAIL, e.returncode)
    self.assertNotIn('v8_suppressions.js', e.output)


if __name__ == '__main__':
  unittest.main()
