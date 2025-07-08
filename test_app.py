# test_app.py
import pytest
from app import app as flask_app

@pytest.fixture
def app():
    """Create and configure a new app instance for each test."""
    flask_app.config.update({
        "TESTING": True,
    })
    yield flask_app

@pytest.fixture
def client(app):
    """A test client for the app."""
    return app.test_client()

def test_login_page_loads(client):
    """
    تست ۱: بررسی می‌کند که آیا صفحه لاگین (روت اصلی) با موفقیت باز می‌شود یا خیر.
    """
    response = client.get('/')
    assert response.status_code == 200
    
    # --- تغییر در این دو خط ---
    response_text = response.data.decode('utf-8')
    assert "RayaCRM" in response_text
    assert "ورود به سیستم" in response_text

def test_superadmin_login_page_loads(client):
    """
    تست ۲: بررسی می‌کند که صفحه لاگین سوپرادمین به درستی بارگذاری می‌شود.
    """
    response = client.get('/superadmin/login')
    assert response.status_code == 200
    # --- تغییر در این خط ---
    assert "ورود سوپرادمین" in response.data.decode('utf-8')

def test_superadmin_panel_redirects_without_login(client):
    """
    تست ۳: بررسی می‌کند که دسترسی به پنل سوپرادمین بدون لاگین، کاربر را به صفحه لاگین هدایت می‌کند.
    """
    response = client.get('/superadmin', follow_redirects=True)
    assert response.status_code == 200
    # --- تغییر در این خط ---
    assert "ورود سوپرادمین" in response.data.decode('utf-8')