"""Basic connectivity and health check tests."""
import requests


def test_app_creates_successfully(app):
    assert app is not None
    assert app.config['TESTING'] is True


def test_root_returns_404(client):
    """No route at root — should 404."""
    response = client.get('/')
    assert response.status_code == 404


def test_api_menu_is_reachable(client):
    """Verify the API is wired up and responding."""
    response = client.get('/api/menu')
    assert response.status_code == 200
    assert response.content_type == 'application/json'


def test_cors_headers_present(client):
    """Verify CORS headers are set on responses."""
    response = client.get('/api/menu', headers={'Origin': 'http://localhost:5173'})
    assert response.status_code == 200
    # flask-cors adds the header when Origin matches
    assert 'Access-Control-Allow-Origin' in response.headers
