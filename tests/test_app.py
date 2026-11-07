def test_healthcheck(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"

def test_missing_payment_config_is_explicit(client):
    response = client.post("/api/payments", json={"amount": "500"})
    assert response.status_code == 503
    assert "не настроена" in response.get_json()["error"]

def test_amount_validation(client):
    response = client.post("/api/payments", json={"amount": "10"})
    assert response.status_code == 400
    assert "Минимальная" in response.get_json()["error"]

def test_not_found(client):
    assert client.get("/does-not-exist").status_code == 404
