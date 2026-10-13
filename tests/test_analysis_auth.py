import base64
import copy
import datetime as dt
import json
from decimal import Decimal
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa, padding
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption, load_pem_private_key
import pytest
from workflow_tools.analytics import dates, parse_sales, summarize, ga4
from workflow_tools.auth import jwt
from workflow_tools.common import Failure, atomic_bytes, save
from workflow_tools.config import Project
from workflow_tools.runtime import Runtime


def unb64(value):
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


@pytest.mark.parametrize("algorithm", ["ES256", "RS256"])
def test_jwt_real_signatures(algorithm, apple_key):
    raw = apple_key if algorithm == "ES256" else rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    token = jwt(raw, {"alg": algorithm, "kid": "test"}, {"iat": 1, "exp": 901})
    header, payload, signature = token.split('.')
    signature = unb64(signature)
    public = load_pem_private_key(raw, None).public_key()
    message = (header + '.' + payload).encode()
    if algorithm == "ES256":
        assert len(signature) == 64
        der = encode_dss_signature(int.from_bytes(signature[:32], 'big'), int.from_bytes(signature[32:], 'big'))
        public.verify(der, message, ec.ECDSA(hashes.SHA256()))
    else:
        public.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(unb64(payload))["exp"] == 901


def test_sales_filters_app_refunds_and_currencies():
    raw = b'SKU\tApple Identifier\tUnits\tDeveloper Proceeds\tCurrency of Proceeds\tParent Identifier\napp\t123\t2\t1.20\tUSD\t\nsub\tother\t-1\t0.50\tUSD\tapp\nother\t999\t100\t50\tUSD\t\napp\t123\t3\t1.00\tEUR\t\n'
    result = parse_sales(raw, '123', 'app', ['sub'])
    assert len(result['rows']) == 3
    assert result['estimated_proceeds_by_currency'] == {'USD': '1.90', 'EUR': '3.00'}
    assert result['net_profit'] is None


def dataset(rows, limited=False):
    return {'dataset_id': '0'*32, 'start_date': '2026-09-01', 'end_date': '2026-09-02',
            'sources': {'ga4': {'status': 'ready', 'rows': rows, 'data_limited': limited}}}


def rows(version, started, success, failed):
    return [{'date':'2026-09-01','app_version':version,'event':e,'event_count':n,'active_users':10}
            for e,n in [('start',started),('success',success),('failed',failed)]]


def test_version_isolation_partition_sample_floor_and_no_user_sum():
    config = {'sample_floor':30,'funnels':[{'name':'completion','numerator':'success','denominator':'start','partition_complements':['failed']}]}
    result = summarize(dataset(rows('1.0',100,70,30)+rows('2.0',10,8,2)),config)
    r = {x['app_version']:x for x in result['event_ratios']}
    assert r['1.0']['value'] == .7
    assert r['2.0']['value'] is None and r['2.0']['reason'] == 'insufficient_sample'
    assert result['active_users_additive'] is False


@pytest.mark.parametrize('limited,complements,failed,version,reason', [
    (True,['failed'],30,'1.0','data_limited'),
    (False,[],30,'1.0','population_unverified'),
    (False,['failed'],40,'1.0','population_unverified'),
    (False,['failed'],30,'(not set)','version_unknown')])
def test_unverified_ratios_have_no_numeric_value(limited,complements,failed,version,reason):
    result=summarize(dataset(rows(version,100,70,failed),limited),{'sample_floor':30,'funnels':[{'numerator':'success','denominator':'start','partition_complements':complements}]})
    assert result['event_ratios'][0]['value'] is None
    assert result['event_ratios'][0]['reason'] == reason


def test_dates_validation():
    start,end=dates('2026-09-01','2026-09-28')
    assert (end-start).days == 27
    for args in [('2026-09-28','2026-09-01'),('bad','2026-09-01'),('2099-01-01','2099-01-02')]:
        with pytest.raises(Failure): dates(*args)


def test_ga4_identity_failure_and_pagination_completeness(project, vault, monkeypatch):
    import workflow_tools.analytics as module
    class GoogleStub:
        config={'property_id':'123'}
        def __init__(self,*_): pass
        def stream(self): return '456'
        def headers(self): return {}
    monkeypatch.setattr(module,'Google',GoogleStub)
    class API:
        def __init__(self): self.payloads=[]
        def json(self,url,**kwargs):
            self.payloads.append(copy.deepcopy(kwargs['payload']))
            if len(self.payloads)==1:
                return {'rowCount':2,'rows':[{'dimensionValues':[{'value':'20260901'},{'value':'1.0'},{'value':'start'}],
                    'metricValues':[{'value':'100'},{'value':'20'}]}]}
            return {'rowCount':2,'rows':[]}
    runtime=Runtime(project.root,project.home,http=API())
    with pytest.raises(Failure) as exc:
        ga4(runtime,dt.date(2026,9,1),dt.date(2026,9,2))
    assert exc.value.code=='DATA_INCOMPLETE'
    assert runtime.http.payloads[0]['dimensionFilter']['filter']['stringFilter']['value']=='456'
    assert runtime.http.payloads[1]['offset']=='1'
