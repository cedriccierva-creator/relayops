"""Isolated launcher tests: no systemd mutations, browser, or application imports."""
import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import os
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('launcher', str(Path(__file__).with_name('relayops')))
spec = importlib.util.spec_from_loader(loader.name, loader)
launcher = importlib.util.module_from_spec(spec)
loader.exec_module(launcher)


def loaded(*args, **kwargs):
    return subprocess.CompletedProcess(args, 0, 'loaded\n', '')


class LauncherTests(unittest.TestCase):
    def test_conflict_starts_nothing(self):
        with patch.object(launcher, 'ctl', side_effect=loaded) as ctl, \
             patch.object(launcher, 'active', return_value=False), \
             patch.object(launcher, 'port_free', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '8001'):
                launcher.start()
            self.assertFalse(any(c.args[0] in ('start', 'stop') for c in ctl.call_args_list))

    def test_repeated_start_does_not_start_worker_twice(self):
        with patch.object(launcher, 'ctl', side_effect=loaded) as ctl, \
             patch.object(launcher, 'active', return_value=True), \
             patch.object(launcher, 'healthy', return_value=True), \
             patch.object(launcher.time, 'sleep'), \
             patch.object(launcher, 'open_dashboard') as browser:
            launcher.start()
            self.assertFalse(any(c.args[0] == 'start' for c in ctl.call_args_list))
            browser.assert_called_once()

    def test_start_waits_before_worker_and_browser(self):
        events = []
        states = {u: False for u in launcher.UNITS}
        def ctl(*args, **kwargs):
            if args[0] == 'start':
                states[args[1]] = True
                events.append(args[1])
            return loaded(*args)
        def health(url):
            events.append('ready')
            return True
        with patch.object(launcher, 'ctl', side_effect=ctl), \
             patch.object(launcher, 'active', side_effect=lambda u: states[u]), \
             patch.object(launcher, 'port_free', return_value=True), \
             patch.object(launcher, 'healthy', side_effect=health), \
             patch.object(launcher.time, 'sleep'), \
             patch.object(launcher, 'open_dashboard') as browser:
            launcher.start()
        self.assertEqual(events, [*launcher.UNITS[:2], 'ready', 'ready', launcher.UNITS[2]])
        browser.assert_called_once()

    def test_failure_only_stops_new_services(self):
        states = {u: False for u in launcher.UNITS}
        states[launcher.UNITS[0]] = True
        def ctl(*args, **kwargs):
            return loaded(*args)
        with patch.object(launcher, 'ctl', side_effect=ctl) as control, \
             patch.object(launcher, 'active', side_effect=lambda u: states[u]), \
             patch.object(launcher, 'port_free', return_value=True), \
             patch.object(launcher, 'open_dashboard') as browser:
            with self.assertRaisesRegex(RuntimeError, 'exited'):
                launcher.start()
        stops = [c.args[1] for c in control.call_args_list if c.args[0] == 'stop']
        self.assertEqual(stops, [launcher.UNITS[1]])
        browser.assert_not_called()

    def test_install_generates_units_without_starting(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(base / 'config'),
                                         'XDG_DATA_HOME': str(base / 'data')}), \
                 patch.object(launcher, 'active', return_value=False), \
                 patch.object(launcher, 'node_path', return_value=Path('/usr/bin/node')), \
                 patch.object(launcher, 'ctl', side_effect=loaded) as ctl:
                launcher.install()
            units = list((base / 'config/systemd/user').glob('*.service'))
            self.assertEqual(len(units), 3)
            for unit in units:
                text = unit.read_text()
                self.assertIn('Restart=no', text)
                self.assertNotIn('WorkingDirectory="', text)
                self.assertNotIn('[Install]', text)
                self.assertIn('RELAYOPS_API_PORT=8001', text)
            desktop = (base / 'data/applications/relayops.desktop').read_text()
            self.assertNotIn('@LAUNCHER@', desktop)
            self.assertIn('Terminal=false', desktop)
            self.assertEqual([c.args for c in ctl.call_args_list], [('daemon-reload',)])

    def test_timeout_does_not_open_browser(self):
        with patch.object(launcher, 'ctl', side_effect=loaded), \
             patch.object(launcher, 'active', return_value=True), \
             patch.object(launcher, 'healthy', return_value=False), \
             patch.object(launcher.time, 'monotonic', side_effect=[0, 91]), \
             patch.object(launcher, 'open_dashboard') as browser:
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                launcher.start()
            browser.assert_not_called()

    def test_chrome_flatpak_app_command(self):
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': ''}), \
             patch.object(launcher.shutil, 'which', return_value='/usr/bin/flatpak'), \
             patch.object(launcher.subprocess, 'run', return_value=loaded()), \
             patch.object(launcher.subprocess, 'Popen') as browser:
            browser.return_value.wait.return_value = 0
            launcher.open_dashboard()
            self.assertEqual(browser.call_args.args[0], [
                '/usr/bin/flatpak', 'run', 'com.google.Chrome', '--app=http://127.0.0.1:3002'])

    def test_browser_fallback_when_flatpak_missing(self):
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': ''}), \
             patch.object(launcher.shutil, 'which', return_value=None), \
             patch.object(launcher.subprocess, 'Popen') as browser:
            browser.return_value.wait.return_value = 0
            launcher.open_dashboard()
            self.assertEqual(browser.call_args.args[0], ['/usr/bin/xdg-open', launcher.URL])

    def test_browser_fallback_when_chrome_not_installed(self):
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': ''}), \
             patch.object(launcher.shutil, 'which', return_value='/usr/bin/flatpak'), \
             patch.object(launcher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), \
             patch.object(launcher.subprocess, 'Popen') as browser:
            browser.return_value.wait.return_value = 0
            launcher.open_dashboard()
            self.assertEqual(browser.call_args.args[0], ['/usr/bin/xdg-open', launcher.URL])

    def test_browser_fallback_when_chrome_launch_fails(self):
        with patch.dict(os.environ, {'XDG_RUNTIME_DIR': ''}), \
             patch.object(launcher.shutil, 'which', return_value='/usr/bin/flatpak'), \
             patch.object(launcher.subprocess, 'run', return_value=loaded()), \
             patch.object(launcher.subprocess, 'Popen') as browser:
            browser.return_value.wait.side_effect = [1, 0]
            launcher.open_dashboard()
            self.assertEqual(browser.call_count, 2)
            self.assertEqual(browser.call_args.args[0], ['/usr/bin/xdg-open', launcher.URL])

    def test_browser_debounce_expires(self):
        with tempfile.TemporaryDirectory() as runtime, \
             patch.dict(os.environ, {'XDG_RUNTIME_DIR': runtime}), \
             patch.object(launcher.shutil, 'which', return_value=None), \
             patch.object(launcher.subprocess, 'Popen') as browser, \
             patch.object(launcher.time, 'monotonic', side_effect=[100, 102, 106, 106]):
            browser.return_value.wait.return_value = 0
            launcher.open_dashboard()
            launcher.open_dashboard()
            self.assertEqual(browser.call_count, 1)
            launcher.open_dashboard()
            self.assertEqual(browser.call_count, 2)

    def test_systemd_quote(self):
        self.assertEqual(launcher.quote('/a b/%x/$y'), '"/a b/%%x/$$y"')


if __name__ == '__main__':
    unittest.main()
