"""Real loopback HTTP checks with injected services; no hardware client exists."""
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from flakefinder.model import SafetyError, AcquisitionError
from flakefinder.suite_server import serve_suite
from test_suite_server import Catalog


class Service:
    def __init__(self):
        self.calls = []
        self.errors = {}

    def __getattr__(self, operation):
        def call(*args, **kwargs):
            self.calls.append((operation, args, kwargs))
            if operation in self.errors:
                raise self.errors[operation]
            return {'operation': operation, 'args': list(args), 'kwargs': kwargs}
        return call


@pytest.fixture
def site(tmp_path):
    revisit, thickness = Service(), Service()
    server, url = serve_suite(tmp_path, 0, provider=Catalog(tmp_path),
                              revisit_service=revisit, thickness_service=thickness)
    with urlopen(url+'api/session') as response:
        session = json.load(response)
    yield url, session, revisit, thickness
    server.shutdown()
    server.server_close()


def get(url):
    with urlopen(url) as response:
        return json.load(response)


def request(site, operation, body, *, token=True, origin=True,
            content_type='application/json', raw=False):
    url, session, _, _ = site
    headers = {'Content-Type': content_type}
    if token:
        headers['X-Review-Token'] = session['token'] if token is True else token
    if origin:
        headers['Origin'] = url.rstrip('/') if origin is True else origin
    data = body if raw else json.dumps(body).encode()
    req = Request(url+'api/revisit/'+operation, data=data, headers=headers)
    with urlopen(req) as response:
        return json.load(response)


def test_read_routes_forward_identity_without_beginning_or_moving(site):
    url, session, revisit, thickness = site
    assert session['automatic_hardware_connection'] is False
    assert 'read_only_hardware' not in session
    assert b'<html' in urlopen(url+'revisit').read().lower()
    query = urlencode({'run': 'scan-example', 'observation': 'chip001/tile_000215/38'})
    assert get(url+'api/revisit/metadata?'+query)['args'] == [
        'scan-example', 'chip001/tile_000215/38']
    assert get(url+'api/revisit/status?session_id=session-1')['args'] == ['session-1']
    assert get(url+'api/revisit/status')['args'] == [None]
    assert get(url+'api/revisit/status?session_id=')['args'] == [None]
    assert get(url+'api/graphene/thickness?'+query)['args'] == [
        'scan-example', 'chip001/tile_000215/38']
    assert [c[0] for c in revisit.calls] == ['metadata', 'status', 'status', 'status']
    assert [c[0] for c in thickness.calls] == ['data']
    with pytest.raises(HTTPError) as error:
        get(url+'api/revisit/move?session_id=session-1')
    assert error.value.code == 404
    assert len(revisit.calls) == 4


@pytest.mark.parametrize('token,origin', [
    (False, True), (True, False), ('wrong-token', True),
    (True, 'https://untrusted.example'),
])
def test_untrusted_mutation_rejected_before_any_service_call(site, token, origin):
    with pytest.raises(HTTPError) as error:
        request(site, 'begin', {'run_id': 'scan-example'}, token=token, origin=origin)
    assert error.value.code == 403
    assert site[2].calls == [] and site[3].calls == []


@pytest.mark.parametrize('operation,body,args,kwargs', [
    ('begin', {'run_id': 'scan-example', 'observation': 'chip001/tile_1/2',
               'placement_mode': 'new', 'operator_confirmed': True,
               'limits': {'x_min': 1, 'x_max': 10}, 'duration_s': 360},
     ('scan-example', 'chip001/tile_1/2'),
     {'placement_mode': 'new', 'operator_confirmed': True,
      'limits': {'x_min': 1, 'x_max': 10}, 'duration_s': 360}),
    ('anchor', {'session_id': 's1', 'corner_id': 'corner_2'}, ('s1', 'corner_2'),
     {'source_observation': None, 'source_field': None, 'source_pixel_xy': None}),
    ('anchor', {'session_id': 's1', 'source_observation': 'chip001/tile_945/8'},
     ('s1', None), {'source_observation': 'chip001/tile_945/8', 'source_field': None,
                    'source_pixel_xy': None}),
    ('anchor', {'session_id': 's1', 'source_field': 'chip001/tile_000945',
                'source_pixel_xy': [1870.5, 1852.25]}, ('s1', None),
     {'source_observation': None, 'source_field': 'chip001/tile_000945',
      'source_pixel_xy': [1870.5, 1852.25]}),
    ('register', {'session_id': 's1', 'orientation_unchanged': True},
     ('s1',), {'orientation_unchanged': True}),
    ('move', {'session_id': 's1', 'observation': 'chip001/tile_2/7'},
     ('s1', 'chip001/tile_2/7'), {}),
    ('stop', {'session_id': 's1'}, ('s1',), {}),
    ('close', {'session_id': 's1'}, ('s1',), {}),
    ('release', {'session_id': 's1'}, ('s1',), {}),
    ('reconnect', {'session_id': 's1', 'operator_confirmed': True},
     ('s1',), {'operator_confirmed': True}),
])
def test_authorized_operations_forward_arguments_exactly(site, operation, body, args, kwargs):
    result = request(site, operation, body)
    assert result['operation'] == operation
    assert site[2].calls == [(operation, args, kwargs)]
    assert site[3].calls == []


def test_omitted_confirmation_is_not_upgraded_to_true(site):
    request(site, 'begin', {'run_id': 'r', 'observation': 'o'})
    request(site, 'register', {'session_id': 's'})
    request(site, 'reconnect', {'session_id': 's'})
    assert site[2].calls == [
        ('begin', ('r', 'o'), {'placement_mode': None, 'operator_confirmed': False,
                              'limits': None, 'duration_s': 1800}),
        ('register', ('s',), {'orientation_unchanged': False}),
        ('reconnect', ('s',), {'operator_confirmed': False}),
    ]


@pytest.mark.parametrize('payload,kind', [(b'[]', 'application/json'),
    (b'null', 'application/json'), (b'{', 'application/json'),
    (b'{}', 'text/plain')])
def test_bad_payload_is_400_without_calling_service(site, payload, kind):
    with pytest.raises(HTTPError) as error:
        request(site, 'move', payload, content_type=kind, raw=True)
    assert error.value.code == 400
    assert site[2].calls == []


@pytest.mark.parametrize('error_type', [SafetyError, AcquisitionError])
def test_control_failure_returns_conflict_and_no_followup_operation(site, error_type):
    site[2].errors['move'] = error_type('Fresh placement verification required')
    with pytest.raises(HTTPError) as error:
        request(site, 'move', {'session_id': 's', 'observation': 'o'})
    assert error.value.code == 409
    assert json.load(error.value)['error'] == 'Fresh placement verification required'
    assert site[2].calls == [('move', ('s', 'o'), {})]


def test_read_safety_error_also_returns_conflict(site):
    site[2].errors['metadata'] = SafetyError('Saved source changed')
    with pytest.raises(HTTPError) as error:
        get(site[0]+'api/revisit/metadata?run=r&observation=o')
    assert error.value.code == 409
    assert site[2].calls == [('metadata', ('r', 'o'), {})]


def test_unknown_operation_is_404_without_service_calls(site):
    with pytest.raises(HTTPError) as error:
        request(site, 'arbitrary-motion', {'session_id': 's'})
    assert error.value.code == 404
    assert site[2].calls == []
