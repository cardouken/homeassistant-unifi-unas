import json


def fake_connection(status=200, body=b"", error=None, calls=None, requests=None):
    class FakeResponse:
        def __init__(self, method, path):
            self.status = status
            self.body = body(method, path) if callable(body) else body

        def read(self):
            return self.body

    class FakeConnection:
        def __init__(self, host, port, timeout=None):
            if calls is not None:
                calls.append((host, port))

        def request(self, method, path, body=None, headers=None):
            if error:
                raise error
            if requests is not None:
                requests.append((method, path, body, headers))
            self.response = FakeResponse(method, path)

        def getresponse(self):
            return self.response

        def close(self):
            pass

    return FakeConnection


def json_body(data):
    return json.dumps(data).encode()
