from django.test import SimpleTestCase

from deploy.check_caddy import verify_build_info


class CaddyBuildSecurityTests(SimpleTestCase):
    def build_info(self, toolchain):
        return f'go\t{toolchain}\npath\tcaddy\ndep\tgithub.com/caddyserver/caddy/v2\tv2.11.4\th1:fixture\n'

    def test_rejects_affected_live_runtime_and_unverifiable_builds(self):
        for version in ['go1.26.3', 'go1.26.5', 'go1.25.12', 'go1.27rc2', 'devel go1.28']:
            with self.subTest(version=version), self.assertRaises(ValueError):
                verify_build_info(self.build_info(version))
        with self.assertRaises(ValueError):
            verify_build_info('v2.11.4')

    def test_accepts_patched_stable_binary_build_information(self):
        for version in ['1.26.6', '1.26.8', '1.27.1']:
            with self.subTest(version=version):
                result = verify_build_info(self.build_info('go' + version))
                self.assertEqual(result, {'caddy_module': 'v2.11.4', 'go': version, 'tls_keyupdate_fix': True})

    def test_rejects_version_claim_without_caddy_release_module(self):
        with self.assertRaises(ValueError):
            verify_build_info('go\tgo1.26.8\npath\tother-server\n')

    def test_verifies_source_build_without_linker_metadata(self):
        info = 'go\tgo1.26.8\nmod\tgithub.com/caddyserver/caddy/v2\t(devel)\n'
        self.assertEqual(verify_build_info(info), {
            'caddy_module': '(devel)', 'go': '1.26.8', 'tls_keyupdate_fix': True,
        })
