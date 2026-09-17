"""Network-free packaging contracts; real regeneration is a separate CI gate."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def check_links(self, doc, boundary):
        for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', doc.read_text()):
            if '://' in target or target.startswith('mailto:'):
                continue
            name, _, anchor = target.partition('#')
            path = (doc.parent / name).resolve() if name else doc.resolve()
            self.assertTrue(path.is_relative_to(boundary.resolve()), target)
            self.assertTrue(path.is_file(), f'{doc}: {target}')
            if anchor:
                headings = re.findall(r'^#+ (.+)$', path.read_text(), re.M)
                slugs = [re.sub(r'[^\w -]', '', h).lower().replace(' ', '-')
                         for h in headings]
                self.assertIn(anchor, slugs, target)

    def test_skill_and_license(self):
        skill = ROOT / 'skills/shepherd-pr/SKILL.md'
        text = skill.read_text()
        self.assertTrue(text.startswith('---\n'))
        front, _ = text[4:].split('\n---\n', 1)
        self.assertLessEqual(len(front), 1024)
        self.assertEqual(len(front.splitlines()), 2)
        fields = dict(line.split(': ', 1) for line in front.splitlines())
        self.assertEqual(set(fields), {'name', 'description'})
        self.assertEqual(fields['name'], skill.parent.name)
        self.assertEqual(fields['name'], 'shepherd-pr')
        self.assertTrue(fields['description'].startswith('Use when '))
        self.assertLess(len(fields['description']), 500)
        license_text = (ROOT / 'LICENSE').read_text()
        self.assertTrue(license_text.startswith('MIT License\n'))
        self.assertIn('Copyright (c) 2026 Prime Radiant, Inc.', license_text)
        self.assertIn('Permission is hereby granted, free of charge', license_text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS"', license_text)

    def test_source_and_generated_manifest(self):
        config = (ROOT / 'everyharness.yaml').read_text()
        for line in ('name: shepherd-pr', 'version: 0.3.0', 'license: MIT',
                     'repository: https://github.com/prime-radiant-inc/shepherd-pr'):
            self.assertIn(line, config.splitlines())
        manifest = json.loads((ROOT / '.everyharness/manifest.json').read_text())
        self.assertEqual(manifest['schema'], 1)
        self.assertTrue(manifest['files'])
        for name, entry in manifest['files'].items():
            path = ROOT / name
            self.assertTrue(path.resolve().is_relative_to(ROOT.resolve()), name)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             entry['sha256'], name)
            if entry.get('executable'):
                self.assertTrue(os.access(path, os.X_OK), name)
        package = json.loads((ROOT / 'package.json').read_text())
        self.assertEqual(package['name'], 'shepherd-pr')
        self.assertEqual(package['version'], '0.3.0')
        self.assertEqual(package['license'], 'MIT')
        self.assertNotIn('devDependencies', package)
        self.assertFalse((ROOT / 'package-lock.json').exists())

    def test_claude_marketplace_has_required_owner(self):
        # Claude's real --strict validator rejects a marketplace with no owner.
        marketplace = json.loads((ROOT / '.claude-plugin/marketplace.json').read_text())
        self.assertEqual(marketplace.get('owner'), {'name': 'Prime Radiant, Inc.'})

    def test_install_docs_and_assets(self):
        readme = (ROOT / 'README.md').read_text()
        start, end = '<!-- everyharness:install:start -->', '<!-- everyharness:install:end -->'
        self.assertEqual(readme.count(start), 1)
        self.assertEqual(readme.count(end), 1)
        self.assertLess(readme.index(start), readme.index(end))
        self.assertNotIn('pending packaging', readme.lower())
        self.assertTrue((ROOT / 'docs/support-matrix.md').is_file())
        guides = list((ROOT / 'docs/install').glob('*.md'))
        self.assertGreaterEqual(len(guides), 11)
        for doc in [ROOT / 'README.md', ROOT / 'docs/testing.md',
                    ROOT / 'skills/shepherd-pr/SKILL.md',
                    ROOT / 'docs/support-matrix.md', *guides]:
            self.check_links(doc, ROOT)
        manifest = json.loads((ROOT / '.everyharness/manifest.json').read_text())
        for name in manifest['files']:
            if name.endswith('.md'):
                self.check_links(ROOT / name, ROOT)

    def test_standalone_skill_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / 'shepherd-pr'
            shutil.copytree(ROOT / 'skills/shepherd-pr', bundle)
            entry = bundle / 'references/pr-watch.sh'
            self.assertTrue(os.access(entry, os.X_OK))
            self.assertTrue((entry.parent / 'pr_watch.py').is_file())
            for doc in bundle.rglob('*.md'):
                self.check_links(doc, bundle)
                for code in re.findall(r'```bash\n(.*?)\n```', doc.read_text(), re.S):
                    result = subprocess.run(['bash', '-n'], input=code,
                                            text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(['bash', str(entry), '--help'], cwd=directory,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--repo', result.stdout)


class DriftGateTests(unittest.TestCase):
    def test_detects_tracked_staged_and_new_dotfiles(self):
        gate = ROOT / 'scripts/check-generated.sh'
        self.assertTrue(gate.is_file(), 'deliver the regeneration drift gate')
        for change in ('clean', 'readme', 'staged', 'new-file', 'new-dotfile'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'scripts').mkdir()
                shutil.copy2(gate, root / 'scripts/check-generated.sh')
                # Fixture only: isolate the gate's Git boundary, no Node/network.
                wrapper = root / 'scripts/everyharness.sh'
                actions = {'clean': ':', 'readme': 'echo drift >> README.md',
                           'staged': 'echo drift >> README.md; git add README.md',
                           'new-file': 'echo drift > new-generated.json',
                           'new-dotfile': 'mkdir .new-adapter; echo drift > .new-adapter/plugin.json'}
                wrapper.write_text('#!/usr/bin/env bash\nset -eu\n'
                                   'test "$1" = generate\n' + actions[change] + '\n')
                wrapper.chmod(0o755)
                (root / 'README.md').write_text('baseline\n')
                commands = [['git', 'init', '-q'], ['git', 'add', 'README.md', 'scripts'],
                            ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                             '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture']]
                for command in commands:
                    subprocess.run(command, cwd=root, check=True, capture_output=True)
                result = subprocess.run(['bash', str(root / 'scripts/check-generated.sh')],
                                        cwd=directory, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0 if change == 'clean' else 1,
                                 result.stdout + result.stderr)
                if change != 'clean':
                    expected = {'readme': 'README.md', 'staged': 'README.md',
                                'new-file': 'new-generated.json',
                                'new-dotfile': '.new-adapter/plugin.json'}[change]
                    self.assertIn(expected, result.stdout + result.stderr)


class WrapperTests(unittest.TestCase):
    def test_help_and_usage_need_no_tooling(self):
        for arguments, expected in ((['--help'], 0), ([], 2), (['typo'], 2)):
            result = subprocess.run(['bash', str(ROOT / 'scripts/everyharness.sh'),
                                     *arguments], text=True, capture_output=True)
            self.assertEqual(result.returncode, expected)
            self.assertIn('Node.js >=20', result.stdout + result.stderr)

    def test_cached_identity_cleanliness_and_rebuild(self):
        source = (ROOT / 'scripts/everyharness.sh').read_text()
        pin = '4f7c5e2112583b1a0d25d4d9413bd06f68f6f8b5'
        self.assertIn(f'readonly pin={pin}', source)
        for case in ('clean-build-cache', 'tracked-edit', 'untracked-source',
                     'ignored-source', 'wrong-head', 'wrong-origin', 'old-node'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                tools = root / '.tools/everyharness'
                tools.mkdir(parents=True)
                (root / 'scripts').mkdir()
                (tools / '.gitignore').write_text('dist/\nnode_modules/\n*.tgz\n')
                (tools / 'package.json').write_text('{}\n')

                def git(*arguments):
                    return subprocess.run(['git', *arguments], cwd=tools,
                                          check=True, text=True, capture_output=True).stdout.strip()

                git('init', '-q')
                git('remote', 'add', 'origin', 'https://github.com/prime-radiant-inc/everyharness.git')
                git('add', 'package.json', '.gitignore')
                git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
                fixture_pin = git('rev-parse', 'HEAD')
                wrapper = root / 'scripts/everyharness.sh'
                # Substitute only the fixture commit; exercise real Git identity/status.
                wrapper.write_text(source.replace(pin, fixture_pin))
                (tools / 'dist').mkdir()
                (tools / 'dist/cli.js').write_text('untrusted cached executable\n')
                (tools / 'dist/stale.js').write_text('stale\n')
                (tools / 'node_modules').mkdir()
                (tools / 'node_modules/cache').write_text('cache\n')
                binaries = root / 'bin'
                binaries.mkdir()
                # npm/Node boundary fixtures keep default discovery network-free.
                (binaries / 'npm').write_text(
                    '#!/usr/bin/env bash\nset -eu\necho "$*" >> "$CALL_LOG"\n'
                    'if [[ "$*" == "run build" ]]; then\n'
                    '  mkdir -p dist; echo rebuilt > dist/cli.js\nfi\n')
                (binaries / 'node').write_text(
                    '#!/usr/bin/env bash\nset -eu\n'
                    'if [[ "$1" == -e ]]; then exit "${NODE_STATUS:-0}"; fi\n'
                    'grep -qx rebuilt "$1"\n'
                    'test ! -e "$(dirname "$1")/stale.js"\n'
                    'echo executed rebuilt CLI\n')
                for executable in binaries.iterdir():
                    executable.chmod(0o755)
                env = dict(os.environ, PATH=f'{binaries}:{os.environ["PATH"]}',
                           CALL_LOG=str(root / 'calls'))
                expected = 'Cached source is dirty'
                if case == 'tracked-edit':
                    (tools / 'package.json').write_text('{"dirty": true}\n')
                elif case == 'untracked-source':
                    (tools / 'injected.js').write_text('injected\n')
                elif case == 'ignored-source':
                    (tools / 'unexpected.tgz').write_text('ignored\n')
                elif case == 'wrong-head':
                    wrapper.write_text(source.replace(pin, '0' * 40))
                    expected = 'Cached HEAD differs'
                elif case == 'wrong-origin':
                    git('remote', 'set-url', 'origin', 'https://example.invalid/other.git')
                    expected = 'Cached origin differs'
                elif case == 'old-node':
                    env['NODE_STATUS'] = '1'
                    expected = 'Node.js >=20 is required'
                result = subprocess.run(['bash', str(wrapper), 'validate'], cwd=root,
                                        env=env, text=True, capture_output=True)
                if case == 'clean-build-cache':
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('executed rebuilt CLI', result.stdout)
                    self.assertEqual((root / 'calls').read_text(), 'ci\nrun build\n')
                else:
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(expected, result.stderr)
                    self.assertFalse((root / 'calls').exists(), 'dirty tooling must not execute npm')
