"""Synthetic protocol frames and mocked transports only; no production signals."""
from __future__ import annotations

import asyncio
import base64
import json
import struct
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from xiaomi_ai_bridge.kelvinator_ir import (apply_settings, checksum, decode_learned,
                                           encode_state, settings_from_state, protocol_template)
from xiaomi_ai_bridge.infrared import InfraredAdapter
from xiaomi_ai_bridge.ai import AIResult, ToolCall
from xiaomi_ai_bridge.bridge import Bridge
from xiaomi_ai_bridge.state import RuntimeStatus
from xiaomi_ai_bridge.miot_tools import DeviceRecord


class _SimulatedAI(SimpleNamespace):
    @asynccontextmanager
    async def lease(self):
        yield self


def synthetic_state() -> bytes:
    # Independent fixture: cool/on/24C/fan3, reserved timer & curve bits retained.
    first = bytearray([0x39, 0x08, 0x20, 0x51, 0xA3, 0x04, 0x02, 0x03])
    second = bytearray([0x39, 0x08, 0x20, 0x71, 0x82, 0x06, 0x38, 0x04])
    # Explicit independently calculated checksums: 9+8+0+1+10+0+0+10=38.
    first[7] |= 6 << 4
    # 9+8+0+1+8+0+3+10=39.
    second[7] |= 7 << 4
    return bytes(first + second)


def independent_capture(state: bytes) -> str:
    """Build a captured A567 container using the reference pulse/gap definition."""
    dictionary = [700, 500, 1600, 9000, 4500, 20000, 40000] + [0] * 9
    pairs = []
    for start in [0, 8]:
        pairs.append((3, 4))
        for byte in state[start:start + 4]:
            pairs += [(0, 2 if byte & (1 << bit) else 1) for bit in range(8)]
        pairs += [(0, 1), (0, 2), (0, 1), (0, 5)]
        for byte in state[start + 4:start + 8]:
            pairs += [(0, 2 if byte & (1 << bit) else 1) for bit in range(8)]
        pairs.append((0, 6 if start == 0 else 0))
    return base64.b64encode(struct.pack('<HH16I',0xA567,279,*dictionary) + bytes(gap << 4 | pulse for pulse,gap in pairs)).decode()


class CodecTests(unittest.TestCase):
    def setUp(self):
        self.seed = synthetic_state()
        self.capture = independent_capture(self.seed)

    def test_decode_known_capture_and_check_fields(self):
        self.assertEqual(decode_learned(self.capture), self.seed)
        settings = settings_from_state(self.seed)
        self.assertEqual((settings['power'],settings['mode'],settings['temperature'],settings['fan']), (True,'cool',24,3))
        self.assertTrue(settings['quiet'])
        self.assertEqual(settings['swing_vertical'],3)

    def test_documented_protocol_default_generates_commands_without_capture(self):
        state = protocol_template()
        # Published reset markers, cool/25C/off, and independently calculated sums.
        self.assertEqual(state, bytes.fromhex('01 09 20 50 00 00 00 40 01 09 20 70 00 00 00 40'))
        self.assertEqual(decode_learned(encode_state(state)), state)
        command = apply_settings(state, {'power':True, 'mode':'heat', 'temperature':28, 'fan':5})
        settings = settings_from_state(decode_learned(encode_state(command)))
        self.assertEqual((settings['power'],settings['mode'],settings['temperature'],settings['fan']), (True,'heat',28,5))

    def test_encoder_matches_independent_framing(self):
        code=encode_state(self.seed)
        raw=base64.b64decode(code)
        self.assertEqual(struct.unpack_from('<HH',raw),(0xA567,279))
        self.assertEqual(len(raw),208)
        self.assertEqual(raw[68:],base64.b64decode(self.capture)[68:])
        self.assertEqual(decode_learned(code),self.seed)

    def test_all_modes_temperatures_fans_and_swings_roundtrip(self):
        for mode in ['auto','cool','dry','fan','heat']:
            for temperature in range(16,31):
                for fan in range(6):
                    state=apply_settings(self.seed,{'mode':mode,'temperature':temperature,'fan':fan})
                    result=settings_from_state(decode_learned(encode_state(state)))
                    self.assertEqual(result['mode'],mode)
                    self.assertEqual(result['temperature'],25 if mode in {'auto','dry'} else temperature)
                    self.assertEqual(result['fan'],fan)
        for swing in [0,1,2,3,4,5,6,7,9,11]:
            state=apply_settings(self.seed,{'swing_vertical':swing,'swing_horizontal':False})
            self.assertEqual(settings_from_state(decode_learned(encode_state(state)))['swing_vertical'],swing)
            self.assertEqual(bool(state[0]&64),swing in {1,7,9,11})

    def test_feature_flags_and_reserved_data_are_preserved(self):
        for field in ['power','swing_horizontal','turbo','quiet','light','ion_filter','xfan']:
            for value in [False,True]:
                self.assertEqual(settings_from_state(apply_settings(self.seed,{field:value}))[field],value)
        new=apply_settings(self.seed,{'temperature':30,'power':False})
        for i in [3,5,6,11,13]:self.assertEqual(new[i],self.seed[i])
        self.assertEqual(new[4]&0xE0,self.seed[4]&0xE0)
        self.assertEqual(new[12]&0x7F,self.seed[12]&0x7F)
        self.assertEqual(new[14]&0x8F,self.seed[14]&0x8F)
        self.assertEqual(new[7]&15,self.seed[7]&15)
        self.assertEqual(new[15]&15,self.seed[15]&15)
        self.assertFalse(settings_from_state(new)['power'])
        self.assertEqual(new[8:11],new[:3])
        self.assertEqual(new[7]>>4,checksum(new[:8]))
        self.assertEqual(new[15]>>4,checksum(new[8:]))

    def test_mode_and_fan_normalization(self):
        state=apply_settings(self.seed,{'turbo':True,'xfan':True})
        self.assertFalse(settings_from_state(apply_settings(state,{'fan':2}))['turbo'])
        self.assertFalse(settings_from_state(apply_settings(state,{'mode':'heat'}))['xfan'])

    def test_unknown_fields_wrong_types_and_ranges_rejected(self):
        for patch in [{},None,{'rpc':'evil'},{'code':'raw'},{'power':'true'}, {'fan':True},
                      {'temperature':15},{'temperature':31},{'temperature':25.5},{'temperature':float('nan')},
                      {'fan':6},{'swing_vertical':8},{'swing_vertical':'1'},{'swing_vertical':True},{'mode':'unknown'}]:
            with self.subTest(patch=patch),self.assertRaises(ValueError):apply_settings(self.seed,patch)

    def test_special_modes_are_exclusive_and_explicit_fan_exits_both(self):
        for feature, other in [('turbo', 'quiet'), ('quiet', 'turbo')]:
            state = apply_settings(self.seed, {other: True})
            state = apply_settings(state, {feature: True})
            active = settings_from_state(state)
            self.assertTrue(active[feature])
            self.assertFalse(active[other])
            for fan in range(6):
                with self.subTest(feature=feature, fan=fan):
                    result = settings_from_state(decode_learned(encode_state(apply_settings(state, {'fan': fan}))))
                    self.assertEqual(result['fan'], fan)
                    self.assertFalse(result['turbo'])
                    self.assertFalse(result['quiet'])
            # Full form/saved snapshots include a fan value. Explicit mode wins.
            snapshot = {**active, 'fan': 5}
            restored = settings_from_state(apply_settings(protocol_template(), snapshot))
            self.assertTrue(restored[feature])
            self.assertFalse(restored[other])
            self.assertEqual(restored['fan'], 5)
            self.assertTrue(settings_from_state(apply_settings(state, {'temperature': 26}))[feature])
        with self.assertRaisesRegex(ValueError, '不能同时开启'):
            apply_settings(self.seed, {'turbo': True, 'quiet': True, 'fan': 5})

    def test_corrupt_and_other_protocol_signals_rejected(self):
        for code in ['', '???', 'A'*9000, base64.b64encode(b'\0'*208).decode()]:
            with self.assertRaises(ValueError):decode_learned(code)
        for index in [0,2,68,104,105,138,174,175]:
            raw=bytearray(base64.b64decode(self.capture));raw[index]^=0x10
            with self.subTest(index=index),self.assertRaises(ValueError):decode_learned(base64.b64encode(raw).decode())
        # A normal Gree 64-bit repeated frame must not become a Kelvinator profile.
        state=bytearray(self.seed);state[8:16]=state[:8];state[11]^=1
        with self.assertRaises(ValueError):decode_learned(independent_capture(bytes(state)))


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.device=DeviceRecord(key='ir-fixture',did='synthetic-did',name='测试红外',alias='测试红外',
                                 model='chuangmi.ir.v2',category='ir-remote-control',enabled=True,cloud_online=True)
        self.home=SimpleNamespace(settings=SimpleNamespace(state_dir=Path(self.temp.name),mi_user_id='synthetic-user'),
                                  devices={self.device.key:self.device},_lock=asyncio.Lock(),_connect=AsyncMock(return_value=object()))
        self.adapter=InfraredAdapter(self.home)
        self.scope=self.adapter._scope(self.device)
        self.seed=synthetic_state()
        self.adapter._learned[self.scope]={'synthetic-profile':{'label':'测试空调','code':independent_capture(self.seed),'frequency':38400}}
        self.adapter._rpc=AsyncMock(return_value=['ok'])
        self.value={'profile_key':'synthetic-profile','settings':{'power':False,'temperature':22}}

    async def test_profiles_are_automatic_and_hide_raw_codes(self):
        self.adapter._learned[self.scope]['bad']={'label':'其他按键','code':'unknown'}
        profiles=self.adapter.ac_profiles(self.device)
        self.assertEqual(len(profiles),1)
        self.assertFalse(profiles[0]['actual_state_known'])
        self.assertFalse(profiles[0]['ai_control_enabled'])
        self.assertNotIn('code',json.dumps(profiles))
        self.assertEqual(self.adapter.tool_definitions(),[])
        self.adapter._rpc.assert_not_called()

    async def test_device_entries_are_local_views_with_a_pending_fallback(self):
        entries = self.adapter.device_entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry['category'], 'air-conditioner')
        self.assertEqual(entry['infrared_ac']['parent_key'], self.device.key)
        self.assertEqual(entry['infrared_ac']['profile_key'], 'synthetic-profile')
        self.assertTrue(entry['infrared_ac']['ready'])
        self.assertEqual(entry['state'], {})
        self.assertFalse(entry['actual_state_known'])
        self.assertNotIn(entry['key'], self.home.devices)
        self.assertNotIn('"code"', json.dumps(entries))
        self.device.cloud_online = False
        self.assertTrue(self.adapter.device_entries()[0]['infrared_ac']['ready'])
        self.adapter._learned[self.scope].clear()
        pending = self.adapter.device_entries()[0]
        self.assertFalse(pending['infrared_ac']['ready'])
        self.assertEqual(pending['name'], '空调')
        self.assertEqual(pending['infrared_ac']['profile_key'], '')
        self.assertIsNone(pending['ac_profile'])
        self.device.enabled = False
        self.assertFalse(self.adapter.device_entries()[0]['enabled'])
        self.home._connect.assert_not_called()
        self.adapter._rpc.assert_not_called()
        self.assertFalse(self.adapter.ac_path.exists())

    async def test_multiple_profiles_remain_separate_and_other_devices_are_ignored(self):
        self.adapter._learned[self.scope]['second'] = {
            'label': '卧室空调关机', 'code': independent_capture(self.seed)}
        self.home.devices['light'] = DeviceRecord(key='light', did='light', name='灯',
            alias='灯', model='yeelink.light.test', category='light', enabled=True)
        entries = self.adapter.device_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(len({entry['key'] for entry in entries}), 2)
        self.assertEqual(entries[1]['name'], '卧室空调')
        self.assertEqual({entry['infrared_ac']['profile_key'] for entry in entries}, {'synthetic-profile', 'second'})

    async def test_invalid_requests_never_emit_without_extra_confirmation(self):
        for value in [{'profile_key':'foreign','settings':{'power':True}},
                      {**self.value,'rpc':'evil'},{'profile_key':'synthetic-profile','settings':{'temperature':50}}]:
            self.assertFalse((await self.adapter.execute(self.device,'ir_ac_send',value))['ok'])
        self.adapter._rpc.assert_not_called()

    async def test_routine_ac_settings_send_directly_but_raw_ir_still_requires_confirmation(self):
        result = await self.adapter.execute(self.device, 'ir_ac_send', self.value)
        self.assertEqual(result['outcome'], 'accepted')
        self.assertFalse(result['actual_state_known'])
        self.adapter._rpc.assert_awaited_once()
        self.adapter._rpc.reset_mock()
        for action in ['ir_send', 'ir_send_learned', 'ir_learn_start', 'ir_learn_delete']:
            result = await self.adapter.execute(self.device, action)
            self.assertEqual(result['outcome'], 'confirmation_required')
        self.adapter._rpc.assert_not_called()

    async def test_protocol_device_is_independent_persistent_and_sends_without_learning(self):
        self.adapter._learned.clear()
        self.device.cloud_online = False
        value = {'protocol':'kelvinator', 'label':'客厅空调'}
        created = await self.adapter.execute(self.device, 'ir_ac_create', value)
        self.assertTrue(created['ok'])
        key = created['profile_key']
        again = await self.adapter.execute(self.device, 'ir_ac_create', value)
        self.assertEqual(again['profile_key'], key)
        self.assertEqual(len(self.adapter.ac_profiles(self.device)), 1)
        self.assertFalse(self.adapter.path.exists())
        self.assertEqual(self.adapter.ac_devices_path.stat().st_mode & 0o777, 0o600)
        profile = self.adapter.ac_profiles(self.device)[0]
        self.assertEqual(profile['source_kind'], 'protocol')
        self.assertEqual(profile['settings_source'], 'protocol_defaults')
        self.assertFalse(profile['requires_learning'])
        self.assertFalse(profile['ai_control_enabled'])
        self.assertTrue(self.adapter.device_entries()[0]['infrared_ac']['ready'])
        self.assertEqual(self.adapter.tool_definitions(), [])
        self.home._connect.assert_not_called()
        self.adapter._rpc.assert_not_called()
        restarted = InfraredAdapter(self.home)
        self.assertEqual(restarted.ac_profiles(self.device), [profile])
        self.device.cloud_online = True
        command = {'profile_key':key, 'settings':{'power':True,'mode':'cool','temperature':26,'fan':4,'swing_horizontal':True}}
        sent = await self.adapter.execute(self.device, 'ir_ac_send', command)
        self.assertEqual(sent['outcome'], 'accepted')
        self.assertFalse(sent['actual_state_known'])
        state = decode_learned(self.adapter._rpc.call_args.args[3]['code'])
        self.assertEqual(settings_from_state(state)['temperature'],26)
        self.assertEqual(settings_from_state(state)['fan'],4)
        self.assertEqual(InfraredAdapter(self.home).ac_profiles(self.device)[0]['settings_source'], 'last_sent')

    async def test_protocol_device_ai_permission_and_account_isolation(self):
        self.adapter._learned.clear()
        created = await self.adapter.execute(self.device, 'ir_ac_create', {'protocol':'kelvinator','label':'空调'})
        key = created['profile_key']
        arguments = {'device':self.device.alias, 'profile_key':key, 'action':'get_settings'}
        self.assertFalse((await self.adapter.execute_tool('control_infrared_ac',arguments))['ok'])
        await self.adapter.execute(self.device,'ir_ac_ai_permission',{'profile_key':key,'enabled':True})
        result = await self.adapter.execute_tool('control_infrared_ac',arguments)
        self.assertEqual(result['settings_source'],'protocol_defaults')
        self.assertTrue(InfraredAdapter(self.home).ac_profiles(self.device)[0]['ai_control_enabled'])
        self.adapter._learned.clear()
        self.assertTrue(self.adapter.ac_profiles(self.device)[0]['ai_control_enabled'])
        self.home.settings.mi_user_id = 'another-synthetic-user'
        self.assertEqual(self.adapter.ac_profiles(self.device),[])
        self.assertFalse((await self.adapter.execute_tool('control_infrared_ac',arguments))['ok'])
        self.adapter._rpc.assert_not_called()

    async def test_conflicting_modes_rejected_for_manual_and_ai_without_emission(self):
        await self.adapter.execute(self.device, 'ir_ac_ai_permission', {'profile_key':'synthetic-profile', 'enabled':True})
        conflict = {'turbo':True, 'quiet':True, 'fan':5}
        result = await self.adapter.execute(self.device, 'ir_ac_send', {'profile_key':'synthetic-profile', 'settings':conflict}, confirmed=True)
        self.assertFalse(result['ok'])
        args = {'device':self.device.alias, 'profile_key':'synthetic-profile', 'action':'set_settings', 'settings':conflict}
        self.assertFalse((await self.adapter.execute_tool('control_infrared_ac', args))['ok'])
        self.adapter._rpc.assert_not_called()
        self.home._connect.assert_not_called()

    async def test_special_modes_persist_and_ai_fan_command_returns_to_normal(self):
        self.adapter._learned.clear()
        created = await self.adapter.execute(self.device, 'ir_ac_create', {'protocol':'kelvinator', 'label':'空调'})
        key = created['profile_key']
        await self.adapter.execute(self.device, 'ir_ac_ai_permission', {'profile_key':key, 'enabled':True})
        for feature in ['turbo', 'quiet']:
            snapshot = {**settings_from_state(protocol_template()), 'fan':4, feature:True}
            result = await self.adapter.execute(self.device, 'ir_ac_send', {'profile_key':key, 'settings':snapshot}, confirmed=True)
            self.assertEqual(result['outcome'], 'accepted')
            self.adapter = InfraredAdapter(self.home)
            self.adapter._rpc = AsyncMock(return_value=['ok'])
            profile = self.adapter.ac_profiles(self.device)[0]
            self.assertTrue(profile['settings'][feature])
            self.assertEqual(profile['settings']['fan'], 4)
            self.assertTrue(profile['ai_control_enabled'])
            args = {'device':self.device.alias, 'profile_key':key, 'action':'set_settings', 'settings':{'fan':4}}
            self.assertEqual((await self.adapter.execute_tool('control_infrared_ac', args))['outcome'], 'accepted')
            sent = settings_from_state(decode_learned(self.adapter._rpc.call_args.args[3]['code']))
            self.assertFalse(sent['turbo'])
            self.assertFalse(sent['quiet'])

    async def test_legacy_conflicting_snapshot_keeps_settings_and_ai_permission(self):
        await self.adapter.execute(self.device, 'ir_ac_ai_permission', {'profile_key':'synthetic-profile', 'enabled':True})
        record = self.adapter._ac_states[self.scope]['synthetic-profile']
        record['settings'] = {**settings_from_state(self.seed), 'temperature':27, 'fan':4, 'turbo':True, 'quiet':True}
        self.adapter._save_ac()
        restarted = InfraredAdapter(self.home)
        restarted._learned = self.adapter._learned
        profile = restarted.ac_profiles(self.device)[0]
        self.assertTrue(profile['ai_control_enabled'])
        self.assertEqual(profile['settings']['temperature'], 27)
        self.assertEqual(profile['settings']['fan'], 4)
        self.assertFalse(profile['settings']['turbo'])
        self.assertFalse(profile['settings']['quiet'])
        self.adapter._rpc.assert_not_called()

    async def test_conflicting_learned_flags_display_normal_mode_without_rewriting_source(self):
        state = bytearray(self.seed)
        state[2] |= 16
        state[10] = state[2]
        for offset in [0, 8]:
            state[offset + 7] = (state[offset + 7] & 15) | checksum(state[offset:offset + 8]) << 4
        code = independent_capture(bytes(state))
        self.adapter._learned[self.scope]['synthetic-profile']['code'] = code
        profile = self.adapter.ac_profiles(self.device)[0]
        self.assertFalse(profile['settings']['turbo'])
        self.assertFalse(profile['settings']['quiet'])
        self.assertEqual(self.adapter._learned[self.scope]['synthetic-profile']['code'], code)
        self.assertFalse(self.adapter.ac_path.exists())
        self.adapter._rpc.assert_not_called()

    async def test_bridge_creates_protocol_device_offline_as_a_local_audited_action(self):
        self.adapter._learned.clear()
        self.device.cloud_online = False
        self.home._audit_result = AsyncMock()
        bridge = Bridge.__new__(Bridge)
        bridge.home = self.home
        bridge.infrared = self.adapter
        bridge.home_extensions = AsyncMock(return_value={'kind':'infrared'})
        result = await bridge._extension_operation(self.device.key, 'ir_ac_create',
            {'protocol':'kelvinator','label':'空调'}, False)
        self.assertTrue(result['ok'])
        self.assertEqual(result['outcome'],'confirmed')
        self.assertTrue(self.adapter.device_entries()[0]['infrared_ac']['ready'])
        self.home._audit_result.assert_awaited_once()
        bridge.home_extensions.assert_awaited_once_with(self.device.key)
        self.home._connect.assert_not_called()
        self.adapter._rpc.assert_not_called()

    async def test_protocol_creation_rejects_unknown_protocol_and_raw_code_and_rolls_back_io_failure(self):
        self.adapter._learned.clear()
        for value in [{'protocol':'unknown','label':'空调'}, {'protocol':'kelvinator','label':'空调','code':'injection'},
                      {'protocol':'kelvinator','label':''}, {'protocol':'kelvinator','label':'空调','frequency':123}]:
            result = await self.adapter.execute(self.device,'ir_ac_create',value)
            self.assertFalse(result['ok'])
        self.assertFalse(self.adapter.ac_devices_path.exists())
        with patch('xiaomi_ai_bridge.infrared._secure_replace', side_effect=OSError('synthetic write failure')):
            result = await self.adapter.execute(self.device,'ir_ac_create',{'protocol':'kelvinator','label':'空调'})
        self.assertFalse(result['ok'])
        self.assertEqual(self.adapter.ac_profiles(self.device),[])
        self.home._connect.assert_not_called()
        self.adapter._rpc.assert_not_called()

    async def test_send_acknowledges_without_faking_actual_state(self):
        result=await self.adapter.execute(self.device,'ir_ac_send',self.value,confirmed=True)
        self.assertEqual(result['outcome'],'accepted');self.assertFalse(result['actual_state_known'])
        params=self.adapter._rpc.call_args.args[3]
        self.assertEqual(params['freq'],38400)
        self.assertEqual(self.adapter._rpc.call_args.args[2],'miIO.ir_play')
        state=decode_learned(params['code']);self.assertFalse(settings_from_state(state)['power'])
        self.assertEqual(settings_from_state(state)['temperature'],22)
        self.assertEqual(self.adapter.ac_profiles(self.device)[0]['settings_source'],'last_sent')
        self.assertEqual(InfraredAdapter(self.home).ac_profiles(self.device),[],'source signals are separate and not copied into preference storage')
        restarted=InfraredAdapter(self.home);restarted._learned=self.adapter._learned
        self.assertEqual(restarted.ac_profiles(self.device)[0]['settings']['temperature'],22)

    async def test_timeout_does_not_retry_or_overwrite_last_settings(self):
        self.adapter._rpc.side_effect=asyncio.TimeoutError
        result=await self.adapter.execute(self.device,'ir_ac_send',self.value,confirmed=True)
        self.assertEqual(result['outcome'],'unconfirmed')
        self.adapter._rpc.assert_awaited_once()
        profile=self.adapter.ac_profiles(self.device)[0]
        self.assertEqual(profile['settings_source'],'learned_signal')
        self.assertEqual(profile['last_attempt']['outcome'],'unconfirmed')
        self.assertEqual(profile['settings']['temperature'],24)

    async def test_explicit_cloud_error_is_failure(self):
        self.adapter._rpc.return_value={'error':{'code':-1}}
        result=await self.adapter.execute(self.device,'ir_ac_send',self.value,confirmed=True)
        self.assertEqual(result['outcome'],'failed')
        self.assertEqual(self.adapter.ac_profiles(self.device)[0]['last_attempt']['outcome'],'failed')

    async def test_offline_permission_is_local_and_revoke_blocks_ai(self):
        self.device.cloud_online=False
        permission={'profile_key':'synthetic-profile','enabled':True}
        self.assertTrue((await self.adapter.execute(self.device,'ir_ac_ai_permission',permission))['ok'])
        self.home._connect.assert_not_called();self.adapter._rpc.assert_not_called()
        self.assertTrue(self.adapter.tool_definitions())
        args={'device':'测试红外','profile_key':'synthetic-profile','action':'get_settings'}
        self.assertTrue((await self.adapter.execute_tool('control_infrared_ac',args))['ok'])
        self.assertFalse((await self.adapter.execute_tool('control_infrared_ac',{**args,'action':'set_settings','settings':{'power':True}}))['ok'])
        self.device.cloud_online=True
        result=await self.adapter.execute_tool('control_infrared_ac',{**args,'action':'set_settings','settings':{'power':True}})
        self.assertEqual(result['outcome'],'accepted')
        await self.adapter.execute(self.device,'ir_ac_ai_permission',{**permission,'enabled':False})
        self.assertFalse((await self.adapter.execute_tool('control_infrared_ac',args))['ok'])
        self.assertEqual(self.adapter._rpc.await_count,1)
        self.assertEqual(self.adapter.tool_definitions(),[])

    async def test_permission_and_device_rechecked_after_connect(self):
        await self.adapter.execute(self.device,'ir_ac_ai_permission',{'profile_key':'synthetic-profile','enabled':True})
        async def revoke():
            self.adapter._ac_states[self.scope]['synthetic-profile']['ai_control_enabled']=False
            return object()
        self.home._connect.side_effect=revoke
        args={'device':'测试红外','profile_key':'synthetic-profile','action':'set_settings','settings':{'power':True}}
        result=await self.adapter.execute_tool('control_infrared_ac',args)
        self.assertFalse(result['ok']);self.adapter._rpc.assert_not_called()

    async def test_device_disabled_and_cross_device_cannot_emit(self):
        self.device.enabled=False
        self.assertFalse((await self.adapter.execute(self.device,'ir_ac_send',self.value,confirmed=True))['ok'])
        self.device.enabled=True
        other=DeviceRecord(key='other',did='other',name='other',alias='other',model='chuangmi.ir.v2',category='ir-remote-control',enabled=True,cloud_online=True)
        self.home.devices['other']=other
        self.assertFalse((await self.adapter.execute(other,'ir_ac_send',self.value,confirmed=True))['ok'])
        self.adapter._rpc.assert_not_called()

    async def test_assistant_dispatches_authorized_ac_tool_and_broadcasts(self):
        await self.adapter.execute(self.device,'ir_ac_ai_permission',{'profile_key':'synthetic-profile','enabled':True})
        self.home.tool_definitions=lambda:[]
        self.home._audit_result=AsyncMock()
        bridge=Bridge.__new__(Bridge)
        bridge.home=self.home;bridge.infrared=self.adapter
        bridge.hub=SimpleNamespace(tool_definitions=lambda:[])
        bridge.health_source=SimpleNamespace(tool_definitions=lambda:[])
        bridge.layout=SimpleNamespace(public=lambda:{'assignments':{}})
        bridge.status=RuntimeStatus();bridge.home_extensions=AsyncMock()
        args={'device':'测试红外','profile_key':'synthetic-profile','action':'set_settings','settings':{'power':True,'temperature':26}}
        bridge.ai=_SimulatedAI(complete=AsyncMock(side_effect=[AIResult(content='',tool_calls=(ToolCall('ac-call','control_infrared_ac',args),)),AIResult(content='空调指令已发送。')]))
        answer=await bridge._answer_with_tools('打开测试空调，设为26度',[],surface='panel')
        self.assertIn('已发送',answer)
        self.adapter._rpc.assert_awaited_once()
        self.home._audit_result.assert_awaited_once()
        bridge.home_extensions.assert_awaited_once_with(self.device.key)
        self.assertEqual(bridge.status.tool_successes,1)
        self.assertEqual(bridge.ai.complete.call_args_list[0].args[1][0]['function']['name'],'control_infrared_ac')

    async def test_ambiguous_assistant_target_requires_clarification(self):
        self.adapter._learned[self.scope]['second-profile']={'label':'卧室空调','code':independent_capture(self.seed)}
        for key in ['synthetic-profile','second-profile']:
            await self.adapter.execute(self.device,'ir_ac_ai_permission',{'profile_key':key,'enabled':True})
        self.home.tool_definitions=lambda:[]
        bridge=Bridge.__new__(Bridge);bridge.home=self.home;bridge.infrared=self.adapter
        bridge.hub=SimpleNamespace(tool_definitions=lambda:[])
        bridge.health_source=SimpleNamespace(tool_definitions=lambda:[])
        bridge.layout=SimpleNamespace(public=lambda:{'assignments':{}})
        bridge.status=RuntimeStatus()
        args={'device':'测试红外','profile_key':'synthetic-profile','action':'set_settings','settings':{'power':True}}
        bridge.ai=_SimulatedAI(complete=AsyncMock(return_value=AIResult(content='',tool_calls=(ToolCall('guess','control_infrared_ac',args),))))
        answer=await bridge._answer_with_tools('打开空调',[],surface='panel')
        self.assertIn('哪台',answer);self.adapter._rpc.assert_not_called()

    async def test_changed_source_drops_permission_and_saved_settings(self):
        await self.adapter.execute(self.device,'ir_ac_ai_permission',{'profile_key':'synthetic-profile','enabled':True})
        new=apply_settings(self.seed,{'temperature':26})
        self.adapter._learned[self.scope]['synthetic-profile']['code']=independent_capture(new)
        profile=self.adapter.ac_profiles(self.device)[0]
        self.assertFalse(profile['ai_control_enabled'])
        self.assertEqual(profile['settings']['temperature'],26)


if __name__=='__main__':unittest.main()
