from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "version": "1.0.0"}

def test_say_hello_valid():
    response = client.post("/hello", json={"name": "Alice"})
    assert response.status_code == 200
    assert response.json() == {"greeting": "Hello, Alice!"}

def test_say_hello_invalid_numbers():
    response = client.post("/hello", json={"name": "Alice123"})
    assert response.status_code == 422
    assert "Value error" in response.text or "Name must not contain numbers" in response.text

def test_say_hello_custom_error():
    response = client.post("/hello", json={"name": "error"})
    assert response.status_code == 400
    assert response.json() == {"detail": "Cannot greet 'error'"}

def test_say_hello_missing_field():
    response = client.post("/hello", json={})
    assert response.status_code == 422
