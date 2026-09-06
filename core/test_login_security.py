import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from .models import AuditEvent
from .views import reserve_login_attempt


TEST_CACHE = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'login-security-tests',
    }
}


def account_key(username):
    return 'login:' + hashlib.sha256(username.strip().lower().encode()).hexdigest()


def source_key(address='127.0.0.1'):
    return 'login-source:' + hashlib.sha256(address.encode()).hexdigest()


@override_settings(
    CACHES=TEST_CACHE,
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    },
)
class LoginSecurityTests(TestCase):
    password = 'Login-regression-test-only-982!'

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user('operator', password=cls.password, is_staff=True)

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post_login(self, password, username='operator', **extra):
        return self.client.post('/login/', {'username': username, 'password': password}, **extra)

    def assert_blocked_without_authentication(self, **extra):
        bodies = []
        with patch('django.contrib.auth.forms.authenticate') as authenticate:
            for password in [self.password, 'incorrect-password']:
                with self.subTest(password_is_correct=password == self.password):
                    response = self.post_login(password, **extra)
                    self.assertEqual(response.status_code, 429)
                    self.assertEqual(response['Retry-After'], '900')
                    self.assertFalse(response.context['form'].is_bound)
                    self.assertFalse(response.context['form'].errors)
                    self.assertNotIn('_auth_user_id', self.client.session)
                    self.assertNotContains(response, password, status_code=429)
                    # CSRF masking is randomized independently on every response.
                    bodies.append(re.sub(
                        rb'name="csrfmiddlewaretoken" value="[^"]+"',
                        b'name="csrfmiddlewaretoken" value="MASKED"',
                        response.content,
                    ))
        authenticate.assert_not_called()
        self.assertEqual(bodies[0], bodies[1])
        self.assertFalse(AuditEvent.objects.filter(action='account.login').exists())

    def test_account_limit_never_checks_correct_or_incorrect_passwords(self):
        for _ in range(10):
            self.assertEqual(self.post_login('incorrect-password').status_code, 200)
        self.assert_blocked_without_authentication()
        self.assertGreaterEqual(cache.get(account_key('operator')), 10)

    def test_source_limit_never_checks_correct_or_incorrect_passwords(self):
        for number in range(80):
            self.assertEqual(self.post_login('incorrect-password', username=f'missing-{number}').status_code, 200)
        self.assert_blocked_without_authentication()
        # A source rejection must not consume the target account's budget.
        self.assertIsNone(cache.get(account_key('operator')))

    def test_account_limit_applies_across_sources_and_username_case(self):
        cache.add(account_key('operator'), 10, timeout=900)
        self.assert_blocked_without_authentication(username=' OPERATOR ', REMOTE_ADDR='192.0.2.17')

    def test_equivalent_unicode_username_cannot_bypass_account_limit(self):
        cache.add(account_key('operator'), 10, timeout=900)
        self.assert_blocked_without_authentication(username='ｏｐｅｒａｔｏｒ', REMOTE_ADDR='192.0.2.17')

    def test_oversized_username_skips_unicode_normalization_like_django(self):
        cache.add(source_key(), 80, timeout=900)
        user_model = get_user_model()
        with patch.object(user_model, 'normalize_username', wraps=user_model.normalize_username) as normalize:
            self.assert_blocked_without_authentication(username='ｏ' * 151)
        normalize.assert_not_called()

    def test_source_limit_uses_the_proxy_appended_address(self):
        cache.add(source_key('192.0.2.18'), 80, timeout=900)
        self.assert_blocked_without_authentication(HTTP_X_FORWARDED_FOR='198.51.100.1, 192.0.2.18')

    def test_successful_final_allowed_attempt_resets_account_but_not_source(self):
        cache.add(account_key('operator'), 9, timeout=900)
        cache.add(source_key(), 79, timeout=900)
        response = self.post_login(self.password)
        self.assertRedirects(response, '/', fetch_redirect_response=False)
        self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))
        self.assertIsNone(cache.get(account_key('operator')))
        self.assertEqual(cache.get(source_key()), 80)
        self.assertTrue(AuditEvent.objects.filter(actor=self.user, action='account.login').exists())

    def test_attempt_is_reserved_before_authentication(self):
        cache.add(account_key('operator'), 9, timeout=900)

        def authentication_check(*args, **kwargs):
            self.assertEqual(cache.get(account_key('operator')), 10)
            self.assertEqual(cache.get(source_key()), 1)

        with patch('django.contrib.auth.forms.authenticate', side_effect=authentication_check) as authenticate:
            self.assertEqual(self.post_login('incorrect-password').status_code, 200)
        authenticate.assert_called_once()
        self.assertEqual(cache.get(account_key('operator')), 10)

    def test_blocked_attempts_do_not_extend_window_and_login_works_after_expiry(self):
        with patch('django.core.cache.backends.base.time.time', return_value=10000):
            cache.add(account_key('operator'), 10, timeout=900)
            cache.add(source_key(), 80, timeout=900)
        with patch('django.core.cache.backends.base.time.time', return_value=10899):
            self.assert_blocked_without_authentication()
        with patch('django.core.cache.backends.base.time.time', return_value=10901):
            response = self.post_login(self.password)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.client.session['_auth_user_id'], str(self.user.pk))
            self.assertEqual(cache.get(source_key()), 1)

    def test_login_get_remains_unbound_without_consuming_attempts(self):
        with patch('django.contrib.auth.forms.authenticate') as authenticate:
            response = self.client.get('/login/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['form'].is_bound)
        authenticate.assert_not_called()
        self.assertIsNone(cache.get(source_key()))


@override_settings(CACHES=TEST_CACHE)
class LoginReservationTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_concurrent_attempts_reserve_exactly_the_limit(self):
        with ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(lambda _: reserve_login_attempt('concurrent', 10), range(100)))
        self.assertEqual(sum(results), 10)
        self.assertEqual(cache.get('concurrent'), 100)

    def test_first_attempt_starts_fixed_window_without_refresh_on_later_attempts(self):
        with patch('django.core.cache.backends.base.time.time', return_value=10000):
            self.assertTrue(reserve_login_attempt('window', 1))
        with patch('django.core.cache.backends.base.time.time', return_value=10899):
            self.assertFalse(reserve_login_attempt('window', 1))
        with patch('django.core.cache.backends.base.time.time', return_value=10901):
            self.assertTrue(reserve_login_attempt('window', 1))
            self.assertEqual(cache.get('window'), 1)

    def test_expiry_between_atomic_operations_retries_reservation(self):
        with (
            patch('core.views.cache.add', side_effect=[False, True]) as add,
            patch('core.views.cache.incr', side_effect=ValueError('expired')),
        ):
            self.assertTrue(reserve_login_attempt('expiring', 10))
        self.assertEqual(add.call_count, 2)

    def test_redis_expiry_during_increment_restores_finite_lifetime(self):
        with (
            patch('core.views.cache.add', return_value=False),
            patch('core.views.cache.incr', return_value=1),
            patch('core.views.cache.touch') as touch,
        ):
            self.assertTrue(reserve_login_attempt('expiring', 10))
        touch.assert_called_once_with('expiring', timeout=900)

    def test_repeated_expiry_fails_closed_without_unbounded_retry(self):
        with (
            patch('core.views.cache.add', return_value=False) as add,
            patch('core.views.cache.incr', side_effect=ValueError('expired')),
        ):
            self.assertFalse(reserve_login_attempt('expiring', 10))
        self.assertEqual(add.call_count, 3)
