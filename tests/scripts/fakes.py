import json


def fake_connection(status=200, body=b"", error=None, calls=None):
    class FakeResponse:
        def __init__(self):
            self.status = status

        def read(self):
            return body

    class FakeConnection:
        def __init__(self, host, port, timeout=None):
            if calls is not None:
                calls.append((host, port))

        def request(self, method, path, headers=None):
            if error:
                raise error

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    return FakeConnection


def json_body(data):
    return json.dumps(data).encode()
