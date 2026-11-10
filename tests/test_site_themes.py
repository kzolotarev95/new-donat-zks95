import re
import json
import struct
from types import SimpleNamespace
from urllib.parse import urljoin

import pytest

from app.backup import export_backup, import_backup
from app.db import create_donor, get_payment
from app.payment_limits import save_payment_limits
from app.settings import get_runtime_setting, set_runtime_setting


def _login(client):
    page = client.get('/admin/login').get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    assert client.post('/admin/login', data={
        'csrf_token': token, 'username': 'admin', 'password': 'correct horse battery staple',
    }).status_code == 302
    page = client.get('/admin/settings').get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', page)[1]


def test_classic_theme_is_preserved_by_default(client):
    body = client.get('/donate').get_data(as_text=True)
    assert 'theme-classic' in body
    assert 'cyber.css' not in body
    assert 'cyber-hero' not in body
    assert '<footer' not in body


def test_admin_can_switch_both_themes_and_choice_is_global(app, client):
    csrf = _login(client)
    settings = client.get('/admin/settings').get_data(as_text=True)
    assert settings.index('Глобальный эффект') < settings.index('Темы сайта')
    assert settings.count('name="SITE_THEME"') == 2
    with app.app_context():
        set_runtime_setting('YOOKASSA_SECRET_KEY', 'test-preserved-secret')
    for theme in ('cyber', 'classic'):
        assert client.post('/admin/settings', data={'csrf_token': csrf, 'SITE_THEME': theme}).status_code == 302
        with app.app_context():
            assert get_runtime_setting('SITE_THEME') == theme
            assert get_runtime_setting('YOOKASSA_SECRET_KEY') == 'test-preserved-secret'
        visitor = app.test_client()
        body = visitor.get('/donate').get_data(as_text=True)
        assert f'theme-{theme}' in body
        assert ('cyber.css' in body) == (theme == 'cyber')
        assert f'theme-{theme}' in client.get('/admin/settings').get_data(as_text=True)


def test_theme_cannot_be_changed_by_visitor_or_without_csrf(app, client):
    assert client.post('/admin/settings', data={'SITE_THEME': 'cyber'}).status_code == 400
    _login(client)
    assert client.post('/admin/settings', data={'SITE_THEME': 'cyber'}).status_code == 400
    with app.app_context():
        assert get_runtime_setting('SITE_THEME', 'classic') == 'classic'


def test_unknown_imported_theme_falls_back_to_classic(app, client):
    with app.app_context():
        set_runtime_setting('SITE_THEME', '../../anything<script>')
    body = client.get('/donate').get_data(as_text=True)
    assert 'theme-classic' in body
    assert '../../anything' not in body


def test_cyber_home_screen_icons_and_launch_destinations(app, client):
    with app.app_context():
        set_runtime_setting('SITE_THEME', 'cyber')
    for page, destination, scope in (('/donate', '/donate', '/'), ('/admin/login', '/admin/', '/admin/')):
        body = client.get(page).get_data(as_text=True)
        manifest_url = re.search(r'rel="manifest" href="([^"]+)"', body)[1]
        manifest = client.get(manifest_url).get_json()
        assert manifest['id'] == manifest['start_url'] == destination
        assert manifest['scope'] == scope
        assert manifest['display'] == 'standalone'
        for icon in manifest['icons']:
            response = client.get(urljoin(manifest_url, icon['src']))
            assert response.status_code == 200
            assert response.data.startswith(b'\x89PNG\r\n\x1a\n')
            assert tuple(map(int, icon['sizes'].split('x'))) == struct.unpack('>II', response.data[16:24])
        apple_url = re.search(r'rel="apple-touch-icon" sizes="180x180" href="([^"]+)"', body)[1]
        apple_icon = client.get(apple_url)
        assert apple_icon.status_code == 200
        assert struct.unpack('>II', apple_icon.data[16:24]) == (180, 180)
        ico_url = re.search(r'rel="alternate icon" type="image/x-icon" href="([^"]+)"', body)[1]
        ico = client.get(ico_url)
        assert ico.status_code == 200
        reserved, image_type, count = struct.unpack('<HHH', ico.data[:6])
        assert reserved == 0 and image_type == 1 and count >= 3


def test_theme_is_in_portable_backup(app):
    with app.app_context():
        set_runtime_setting('SITE_THEME', 'cyber')
        payload = export_backup('full')
        set_runtime_setting('SITE_THEME', 'classic')
        import_backup(payload)
        assert get_runtime_setting('SITE_THEME') == 'cyber'


@pytest.mark.parametrize('theme', ['classic', 'cyber'])
def test_public_and_admin_pages_render_with_either_theme(app, client, theme, monkeypatch):
    with app.app_context():
        set_runtime_setting('SITE_THEME', theme)
    monkeypatch.setattr('app.routes.fetch_repositories', lambda *_args: [])
    for path in ('/donate', '/about', '/services', '/support', '/projects', '/contact', '/faq',
                 '/requisites', '/privacy', '/terms', '/payment/success', '/admin/login'):
        response = client.get(path)
        assert response.status_code == 200, path
        assert f'theme-{theme}' in response.get_data(as_text=True), path
    _login(client)
    for path in ('/admin/', '/admin/settings', '/admin/donors', '/admin/payments',
                 '/admin/notifications', '/admin/account', '/admin/backup'):
        assert client.get(path).status_code == 200, path


def test_cyber_uses_real_donors_preserves_pins_and_hides_after_twelve(app, client):
    with app.app_context():
        set_runtime_setting('SITE_THEME', 'cyber')
        create_donor('Actual donor', 'c042', 'Actual note', '1250.50', 'https://t.me/example', pinned=1)
        for index in range(12):
            create_donor(f'Donor {index}', 'blue', total_amount='10')
    body = client.get('/donate').get_data(as_text=True)
    assert 'Actual donor' in body and '1 250.50' in body and 'Actual note' in body
    assert 'https://t.me/example' in body and 'data-pinned="true"' in body
    assert body.count('data-extra-donor') == 1
    assert 'data-show-all-donors' in body and 'donor-show-all' in body
    assert '/avatar.svg' not in body


@pytest.mark.parametrize('limits', [(100, 1000000), (1, 100), (9000, 12000), (10, 10)])
def test_cyber_quick_amounts_respect_saved_payment_limits(app, client, limits):
    with app.app_context():
        set_runtime_setting('SITE_THEME', 'cyber')
        save_payment_limits(*limits)
    body = client.get('/donate').get_data(as_text=True)
    amounts = [int(value) for value in re.findall(r'data-amount="(\d+)"', body)]
    assert amounts and all(limits[0] <= value <= limits[1] for value in amounts)
    if limits == (100, 1000000):
        assert amounts == [500, 1000, 2000, 5000]
    assert f'min="{limits[0]}" max="{limits[1]}"' in body
    assert 'name="nickname"' in body and 'name="message"' in body
    assert 'img/sbp-logo.svg' in body
    assert 'bank-chip' not in body


def test_missing_theme_on_older_settings_form_does_not_reset_choice(app, client):
    csrf = _login(client)
    with app.app_context():
        set_runtime_setting('SITE_THEME', 'cyber')
    assert client.post('/admin/settings', data={'csrf_token': csrf, 'VISUAL_EFFECT': 'snow'}).status_code == 302
    with app.app_context():
        assert get_runtime_setting('SITE_THEME') == 'cyber'


@pytest.mark.parametrize('theme', ['classic', 'cyber'])
def test_payment_creation_and_verified_webhook_work_in_both_themes(app, client, monkeypatch, theme):
    app.config.update(YOOKASSA_SHOP_ID='test-shop', YOOKASSA_SECRET_KEY='test-key')
    captured = {}

    class Gateway:
        @staticmethod
        def create(payload, idempotency_key):
            captured.update(payload=payload, idempotency_key=idempotency_key)
            return SimpleNamespace(
                id='payment-theme-test', status='pending',
                amount=SimpleNamespace(value=payload['amount']['value'], currency='RUB'),
                confirmation=SimpleNamespace(confirmation_url='https://checkout.example.test/sbp'),
                json=lambda: json.dumps({'id':'payment-theme-test', 'status':'pending'}),
            )

        @staticmethod
        def find_one(payment_id):
            return SimpleNamespace(
                id=payment_id, status='succeeded', amount=SimpleNamespace(value='2000.00', currency='RUB'),
                json=lambda: json.dumps({'id':payment_id, 'status':'succeeded'}),
            )

    monkeypatch.setattr('app.payments._sdk', lambda: Gateway)
    with app.app_context():
        set_runtime_setting('SITE_THEME', theme)
    response = client.post('/api/payments', json={'amount':'2000', 'nickname':'Actual user', 'message':'Keep building'})
    assert response.status_code == 200
    assert captured['payload']['payment_method_data'] == {'type':'sbp'}
    assert captured['payload']['amount'] == {'value':'2000.00', 'currency':'RUB'}
    assert captured['payload']['metadata'] == {'nickname':'Actual user', 'message':'Keep building'}
    assert 'payment_ref=' in captured['payload']['confirmation']['return_url']
    assert client.post('/webhooks/yookassa', json={'object':{'id':'payment-theme-test'}}).status_code == 200
    with app.app_context():
        record = get_payment('payment-theme-test')
        assert record['status'] == 'succeeded'
        assert record['customer_nickname'] == 'Actual user' and record['message'] == 'Keep building'
    assert 'Payment received' in client.get('/payment/success?payment_id=payment-theme-test').get_data(as_text=True)
