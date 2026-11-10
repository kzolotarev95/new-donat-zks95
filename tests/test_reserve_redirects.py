"""The admin entry must keep the browser on its original HTTPS port."""
from urllib.parse import urljoin, urlsplit

import pytest


@pytest.mark.parametrize('origin', ['https://donate.example:8443', 'https://donate.example',
                                  'https://new-domain.example:9443'])
def test_admin_entry_uses_uncached_relative_redirect(client, origin):
    response = client.get('/admin', base_url=origin)
    assert response.status_code == 302
    assert response.headers['Location'] == '/admin/'
    assert 'no-store' in response.headers['Cache-Control']
    target = urljoin(origin + '/admin', response.headers['Location'])
    assert urlsplit(target).netloc == urlsplit(origin).netloc
    assert urlsplit(target).scheme == 'https'


def test_reserve_admin_entry_reaches_login_without_leaving_origin(client):
    response = client.get('/admin', base_url='https://donate.example:8443', follow_redirects=True)
    assert response.status_code == 200
    assert response.request.path == '/admin/login'
    assert response.request.host == 'donate.example:8443'
    assert response.request.scheme == 'https'
    assert len(response.history) == 2
    assert all(step.status_code == 302 for step in response.history)
    assert all(step.headers['Location'].startswith('/admin/') for step in response.history)
    assert all('no-store' in step.headers['Cache-Control'] for step in response.history)
