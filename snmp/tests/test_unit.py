# (C) Datadog, Inc. 2018-present
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)

import copy
import os
import time
import weakref
from concurrent import futures
from typing import Any, List

import mock
import pytest
import yaml

from datadog_checks.base import ConfigurationError
from datadog_checks.dev import temp_dir
from datadog_checks.snmp import SnmpCheck
from datadog_checks.snmp.config import InstanceConfig
from datadog_checks.snmp.discovery import discover_instances
from datadog_checks.snmp.parsing import ParsedSymbolMetric, ParsedTableMetric
from datadog_checks.snmp.resolver import OIDTrie
from datadog_checks.snmp.utils import _load_default_profiles, oid_pattern_specificity, recursively_expand_base_profiles

from . import common
from .utils import mock_profiles_confd_root

pytestmark = pytest.mark.unit


@mock.patch("datadog_checks.snmp.config.lcd")
def test_parse_metrics(lcd_mock):
    # type: (Any) -> None
    lcd_mock.configure.return_value = ('addr', None)

    config = InstanceConfig(
        {"ip_address": "127.0.0.1", "community_string": "public", "metrics": [{"OID": "1.2.3", "name": "foo"}]}
    )

    # Unsupported metric.
    metrics = [{"foo": "bar"}]  # type: list
    with pytest.raises(Exception):
        config.parse_metrics(metrics)

    # Simple OID
    metrics = [{"OID": "1.2.3", "name": "foo"}]
    oids, _, _, parsed_metrics = config.parse_metrics(metrics)
    assert len(oids) == 1
    assert len(parsed_metrics) == 1
    foo = parsed_metrics[0]
    assert isinstance(foo, ParsedSymbolMetric)
    assert foo.name == 'foo'

    # MIB with no symbol or table
    metrics = [{"MIB": "foo_mib"}]
    with pytest.raises(Exception):
        config.parse_metrics(metrics)

    # MIB with symbol
    metrics = [{"MIB": "foo_mib", "symbol": "foo"}]
    oids, _, _, parsed_metrics = config.parse_metrics(metrics)
    assert len(oids) == 1
    assert len(parsed_metrics) == 1
    foo = parsed_metrics[0]
    assert isinstance(foo, ParsedSymbolMetric)
    assert foo.name == 'foo'

    # MIB with table, no symbols
    metrics = [{"MIB": "foo_mib", "table": "foo"}]
    with pytest.raises(Exception):
        config.parse_metrics(metrics)

    # MIB with table and symbols
    metrics = [{"MIB": "foo_mib", "table": "foo_table", "symbols": ["foo", "bar"]}]
    _, next_oids, _, parsed_metrics = config.parse_metrics(metrics)
    assert len(next_oids) == 2
    assert len(parsed_metrics) == 2
    foo, bar = parsed_metrics
    assert isinstance(foo, ParsedTableMetric)
    assert foo.name == 'foo'
    assert isinstance(foo, ParsedTableMetric)
    assert bar.name == 'bar'

    # MIB with table, symbols, bad metrics_tags
    metrics = [{"MIB": "foo_mib", "table": "foo_table", "symbols": ["foo", "bar"], "metric_tags": [{}]}]
    with pytest.raises(Exception):
        config.parse_metrics(metrics)

    # MIB with table, symbols, bad metrics_tags
    metrics = [{"MIB": "foo_mib", "table": "foo_table", "symbols": ["foo", "bar"], "metric_tags": [{"tag": "test"}]}]
    with pytest.raises(Exception):
        config.parse_metrics(metrics)

    # Table with manual OID
    metrics = [{"MIB": "foo_mib", "table": "foo_table", "symbols": [{"OID": "1.2.3", "name": "foo"}]}]
    _, next_oids, _, parsed_metrics = config.parse_metrics(metrics)
    assert len(next_oids) == 1
    assert len(parsed_metrics) == 1
    foo = parsed_metrics[0]
    assert isinstance(foo, ParsedTableMetric)
    assert foo.name == 'foo'

    # MIB with table, symbols, metrics_tags index
    metrics = [
        {
            "MIB": "foo_mib",
            "table": "foo_table",
            "symbols": ["foo", "bar"],
            "metric_tags": [{"tag": "test", "index": "1"}],
        },
    ]
    _, next_oids, _, parsed_metrics = config.parse_metrics(metrics)
    assert len(next_oids) == 2
    assert len(parsed_metrics) == 2
    foo, bar = parsed_metrics
    assert isinstance(foo, ParsedTableMetric)
    assert foo.name == 'foo'
    assert foo.index_tags == [('test', '1')]
    assert isinstance(bar, ParsedTableMetric)
    assert bar.name == 'bar'
    assert bar.index_tags == [('test', '1')]

    # MIB with table, symbols, metrics_tags column
    metrics = [
        {
            "MIB": "foo_mib",
            "table": "foo_table",
            "symbols": ["foo", "bar"],
            "metric_tags": [{"tag": "test", "column": "baz"}],
        }
    ]
    _, next_oids, _, parsed_metrics = config.parse_metrics(metrics)
    assert len(next_oids) == 3
    assert len(parsed_metrics) == 2
    foo, bar = parsed_metrics
    assert isinstance(foo, ParsedTableMetric)
    assert foo.name == 'foo'
    assert foo.column_tags == [('test', 'baz')]
    assert isinstance(bar, ParsedTableMetric)
    assert bar.name == 'bar'
    assert bar.column_tags == [('test', 'baz')]

    # MIB with table, symbols, metrics_tags column with OID
    metrics = [
        {
            "MIB": "foo_mib",
            "table": "foo_table",
            "symbols": ["foo", "bar"],
            "metric_tags": [{"tag": "test", "column": {"name": "baz", "OID": "1.5.6"}}],
        }
    ]
    _, next_oids, _, parsed_metrics = config.parse_metrics(metrics)
    assert len(next_oids) == 3
    assert len(parsed_metrics) == 2
    foo, bar = parsed_metrics
    assert isinstance(foo, ParsedTableMetric)
    assert foo.name == 'foo'
    assert foo.column_tags == [('test', 'baz')]
    assert isinstance(bar, ParsedTableMetric)
    assert bar.name == 'bar'
    assert foo.column_tags == [('test', 'baz')]


def test_ignore_ip_addresses():
    # type: () -> None
    instance = common.generate_instance_config(common.SUPPORTED_METRIC_TYPES)
    instance.pop('ip_address')
    instance['network_address'] = '192.168.1.0/29'
    instance['ignored_ip_addresses'] = ['192.168.1.2', '192.168.1.3', '192.168.1.5']

    check = SnmpCheck('snmp', {}, [instance])
    assert list(check._config.network_hosts()) == ['192.168.1.1', '192.168.1.4', '192.168.1.6']

    instance = common.generate_instance_config(common.SUPPORTED_METRIC_TYPES)
    string_not_in_a_list = '192.168.1.0/29'
    instance['ignored_ip_addresses'] = string_not_in_a_list
    with pytest.raises(ConfigurationError):
        SnmpCheck('snmp', {}, [instance])


def test_empty_values():
    instance = common.generate_instance_config(common.SUPPORTED_METRIC_TYPES)
    instance['user'] = ''
    instance['enforce_mib_constraints'] = ''
    instance['timeout'] = ''
    instance['retries'] = ''
    check = SnmpCheck('snmp', {}, [instance])
    assert check._config.enforce_constraints


def test_profile_error():
    instance = common.generate_instance_config([])
    instance['profile'] = 'profile1'
    with pytest.raises(ConfigurationError):
        SnmpCheck('snmp', {}, [instance])

    init_config = {'profiles': {'profile1': {'definition_file': 'doesntexistfile'}}}
    with pytest.raises(ConfigurationError):
        SnmpCheck('snmp', init_config, [instance])

    with temp_dir() as tmp:
        profile_file = os.path.join(tmp, 'profile1.yaml')
        with open(profile_file, 'w') as f:
            f.write("not yaml: {")
        init_config = {'profiles': {'profile1': {'definition_file': profile_file}}}
        with pytest.raises(ConfigurationError):
            SnmpCheck('snmp', init_config, [instance])


def test_duplicate_sysobjectid_error():
    profile1 = {'sysobjectid': '1.3.6.1.4.1.30932.*'}
    profile2 = copy.copy(profile1)

    instance = common.generate_instance_config([])
    init_config = {'profiles': {'profile1': {'definition': profile1}, 'profile2': {'definition': profile2}}}

    with pytest.raises(ConfigurationError) as e:
        SnmpCheck('snmp', init_config, [instance])
    assert "has the same sysObjectID" in str(e.value)

    # no errors are raised
    init_config['profiles']['profile2']['definition']['sysobjectid'] = '1.3.6.2.4.1.30932.*'
    SnmpCheck('snmp', init_config, [instance])


def test_no_address():
    instance = common.generate_instance_config([])
    instance.pop('ip_address')
    with pytest.raises(ConfigurationError) as e:
        SnmpCheck('snmp', {}, [instance])
    assert str(e.value) == 'An IP address or a network address needs to be specified'


def test_both_addresses():
    instance = common.generate_instance_config(common.SUPPORTED_METRIC_TYPES)
    instance['network_address'] = '192.168.0.0/24'
    with pytest.raises(ConfigurationError) as e:
        SnmpCheck('snmp', {}, [instance])
    assert str(e.value) == 'Only one of IP address and network address must be specified'


def test_removing_host():
    """If a discovered host is failing 3 times in a row, we stop querying it."""
    instance = common.generate_instance_config(common.SUPPORTED_METRIC_TYPES)
    discovered_instance = instance.copy()
    discovered_instance['ip_address'] = '1.1.1.1'
    discovered_instance['retries'] = 0
    instance.pop('ip_address')
    instance['network_address'] = '192.168.0.0/24'
    check = SnmpCheck('snmp', {}, [instance])
    warnings = []
    check.warning = warnings.append
    check._config.discovered_instances['1.1.1.1'] = InstanceConfig(discovered_instance)
    msg = 'Failed to collect some metrics: No SNMP response received before timeout for instance 1.1.1.1'

    check._start_discovery = lambda: None
    check._executor = futures.ThreadPoolExecutor(max_workers=1)
    check.check(instance)
    assert warnings == [msg]

    check.check(instance)
    assert warnings == [msg, msg]

    check.check(instance)
    assert warnings == [msg, msg, msg]
    # Instance has been removed
    assert check._config.discovered_instances == {}

    check.check(instance)
    # No new warnings produced
    assert warnings == [msg, msg, msg]


def test_invalid_discovery_interval():
    instance = common.generate_instance_config(common.SUPPORTED_METRIC_TYPES)

    # Trigger autodiscovery.
    instance.pop('ip_address')
    instance['network_address'] = '192.168.0.0/24'

    instance['discovery_interval'] = 'not_parsable_as_a_float'

    check = SnmpCheck('snmp', {}, [instance])
    with pytest.raises(ConfigurationError):
        check.check(instance)


@mock.patch("datadog_checks.snmp.snmp.read_persistent_cache")
def test_cache_discovered_host(read_mock):
    instance = common.generate_instance_config(common.SUPPORTED_METRIC_TYPES)
    instance.pop('ip_address')
    instance['network_address'] = '192.168.0.0/24'

    read_mock.return_value = '["192.168.0.1"]'
    check = SnmpCheck('snmp', {}, [instance])
    check._thread_factory = lambda **kwargs: mock.Mock()
    check.check(instance)

    assert '192.168.0.1' in check._config.discovered_instances


@mock.patch("datadog_checks.snmp.snmp.read_persistent_cache")
@mock.patch("datadog_checks.snmp.snmp.write_persistent_cache")
def test_cache_corrupted(write_mock, read_mock):
    instance = common.generate_instance_config(common.SUPPORTED_METRIC_TYPES)
    instance.pop('ip_address')
    instance['network_address'] = '192.168.0.0/24'
    read_mock.return_value = '["192.168.0."]'
    check = SnmpCheck('snmp', {}, [instance])
    check._thread_factory = lambda **kwargs: mock.Mock()
    check.check(instance)

    assert not check._config.discovered_instances
    write_mock.assert_called_once_with('', '[]')


@mock.patch("datadog_checks.snmp.snmp.read_persistent_cache")
@mock.patch("datadog_checks.snmp.discovery.write_persistent_cache")
def test_cache_building(write_mock, read_mock):
    instance = common.generate_instance_config(common.SUPPORTED_METRIC_TYPES)
    instance['timeout'] = 1
    instance.pop('ip_address')

    read_mock.return_value = '[]'

    discovered_instance = instance.copy()
    discovered_instance['ip_address'] = '192.168.0.1'

    instance['network_address'] = '192.168.0.0/31'

    check = SnmpCheck('snmp', {}, [instance])

    check._config.discovered_instances['192.168.0.1'] = InstanceConfig(discovered_instance)
    check._start_discovery()

    try:
        for _ in range(30):
            if write_mock.call_count:
                break
            time.sleep(0.5)
    finally:
        check._running = False

    write_mock.assert_called_once_with('', '["192.168.0.1"]')


def test_trie():
    trie = OIDTrie()
    trie.set((1, 2), 'bar')
    trie.set((1, 2, 3), 'foo')
    assert trie.match((1,)) == ((1,), None)
    assert trie.match((1, 2)) == ((1, 2), 'bar')
    assert trie.match((1, 2, 3)) == ((1, 2, 3), 'foo')
    assert trie.match((1, 2, 3, 4)) == ((1, 2, 3), 'foo')
    assert trie.match((2, 3, 4)) == ((), None)


@pytest.mark.parametrize(
    'oids, expected',
    [
        (['1.3.4.1'], ['1.3.4.1']),
        (['1.3.4.*', '1.3.4.1'], ['1.3.4.*', '1.3.4.1']),
        (['1.3.4.1', '1.3.4.*'], ['1.3.4.*', '1.3.4.1']),
        (['1.3.4.1.2', '1.3.4'], ['1.3.4', '1.3.4.1.2']),
        (
            ['1.3.6.1.4.1.3375.2.1.3.4.43', '1.3.6.1.4.1.8072.3.2.10'],
            ['1.3.6.1.4.1.8072.3.2.10', '1.3.6.1.4.1.3375.2.1.3.4.43'],
        ),
    ],
)
def test_oid_pattern_specificity(oids, expected):
    # type: (List[str], List[str]) -> None
    assert sorted(oids, key=oid_pattern_specificity) == expected


def test_profile_extends():
    # type: () -> None
    base = {
        'metrics': [
            {'MIB': 'TCP-MIB', 'symbol': 'tcpActiveOpens', 'forced_type': 'monotonic_count'},
            {'MIB': 'UDP-MIB', 'symbol': 'udpHCInDatagrams', 'forced_type': 'monotonic_count'},
        ],
        'metric_tags': [{'MIB': 'SNMPv2-MIB', 'symbol': 'sysName', 'tag': 'snmp_host'}],
    }

    profile1 = {
        'extends': ['base.yaml'],
        'metrics': [{'MIB': 'TCP-MIB', 'symbol': 'tcpPassiveOpens', 'forced_type': 'monotonic_count'}],
    }

    with temp_dir() as tmp:
        with mock_profiles_confd_root(tmp):
            with open(os.path.join(tmp, 'base.yaml'), 'w') as f:
                f.write(yaml.safe_dump(base))

            with open(os.path.join(tmp, 'profile1.yaml'), 'w') as f:
                f.write(yaml.safe_dump(profile1))

            definition = {'extends': ['profile1.yaml']}

            recursively_expand_base_profiles(definition)

            assert definition == {
                'extends': ['profile1.yaml'],
                'metrics': [
                    {'MIB': 'TCP-MIB', 'symbol': 'tcpActiveOpens', 'forced_type': 'monotonic_count'},
                    {'MIB': 'UDP-MIB', 'symbol': 'udpHCInDatagrams', 'forced_type': 'monotonic_count'},
                    {'MIB': 'TCP-MIB', 'symbol': 'tcpPassiveOpens', 'forced_type': 'monotonic_count'},
                ],
                'metric_tags': [{'MIB': 'SNMPv2-MIB', 'symbol': 'sysName', 'tag': 'snmp_host'}],
            }


def test_default_profiles():
    profile = {
        'metrics': [{'MIB': 'TCP-MIB', 'symbol': 'tcpPassiveOpens', 'forced_type': 'monotonic_count'}],
    }

    with temp_dir() as tmp:
        with mock_profiles_confd_root(tmp):
            profile_file = os.path.join(tmp, 'profile.yaml')
            with open(profile_file, 'w') as f:
                f.write(yaml.safe_dump(profile))

            profiles = _load_default_profiles()
            assert profiles['profile'] == {'definition': profile}


def test_profile_override():
    profile = {
        'metrics': [{'MIB': 'TCP-MIB', 'symbol': 'tcpPassiveOpens', 'forced_type': 'monotonic_count'}],
    }

    with temp_dir() as tmp:
        with mock_profiles_confd_root(tmp):
            profile_file = os.path.join(tmp, 'generic-router.yaml')
            with open(profile_file, 'w') as f:
                f.write(yaml.safe_dump(profile))

            profiles = _load_default_profiles()
            assert profiles['generic-router'] == {'definition': profile}


def test_discovery_tags():
    """When specifying a tag on discovery, it doesn't make tags leaks between instances."""
    instance = common.generate_instance_config(common.SUPPORTED_METRIC_TYPES)
    instance.pop('ip_address')

    instance['network_address'] = '192.168.0.0/29'
    instance['tags'] = ['test:check']

    check = SnmpCheck('snmp', {}, [instance])

    oids = ['1.3.6.1.4.5', '1.3.6.1.4.5']

    def mock_fetch(cfg):
        if oids:
            return oids.pop(0)
        check._running = False
        raise RuntimeError("Not snmp")

    check.fetch_sysobject_oid = mock_fetch

    discover_instances(check._config, 0, weakref.ref(check))

    config = check._config.discovered_instances['192.168.0.2']
    assert set(config.tags) == {'snmp_device:192.168.0.2', 'test:check', 'snmp_profile:generic-router'}


@mock.patch("datadog_checks.snmp.snmp.read_persistent_cache")
@mock.patch("threading.Thread")
def test_cache_loading_tags(thread_mock, read_mock):
    """When loading discovered instances from cache, tags don't leak from one to the others."""
    read_mock.return_value = '["192.168.0.1", "192.168.0.2"]'
    instance = common.generate_instance_config(common.SUPPORTED_METRIC_TYPES)
    instance.pop('ip_address')

    instance['network_address'] = '192.168.0.0/29'
    instance['discovery_interval'] = 0
    instance['tags'] = ['test:check']

    check = SnmpCheck('snmp', {}, [instance])
    check._start_discovery()

    config = check._config.discovered_instances['192.168.0.2']
    assert set(config.tags) == {'snmp_device:192.168.0.2', 'test:check'}


def test_failed_to_collect_metrics():
    config = InstanceConfig(
        {"ip_address": "127.0.0.123", "community_string": "public", "metrics": [{"OID": "1.2.3", "name": "foo"}]}
    )

    instance = common.generate_instance_config(common.SUPPORTED_METRIC_TYPES)
    instance.pop('ip_address')
    instance['network_address'] = '192.168.0.0/24'

    check = SnmpCheck('snmp', {}, [instance])
    check.fetch_results = mock.Mock(return_value=ValueError("invalid value"))

    check._check_with_config(config)

    assert len(check.warnings) == 1
    assert 'Failed to collect metrics for 127.0.0.123' in check.warnings[0]
