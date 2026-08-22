from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

def test_stonks_endpoint():
    payload = [{
     "energy":2,
     "capital":500,
     "timeline":{
       "2037":{
         "Apple":{
           "price":100,
           "qty":10
         }
       },
       "2036":{
         "Apple":{
           "price":10,
           "qty":50
          }
       }
     }
    }]
    response = client.post("/stonks", json=payload)
    assert response.status_code == 200
    assert response.json() == [["j-2037-2036", "b-Apple-50", "j-2036-2037", "s-Apple-50"]]
