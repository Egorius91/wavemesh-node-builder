"""Portable decisions only. POSIX durability/locks/crash require native proof."""
import copy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'agent'))
from heartbeat_observation import heartbeat_node_hash
from managed_observation import ObservationError,digest,make_sample,stamp,validate_manifest,qualify_interval


def manifest():
    return dict(schema_version=1,kind='MANAGED_MTLS_OBSERVATION_RUN_V1',run_id='1'*32,scope='synthetic',
        node_sha256=heartbeat_node_hash('tenant-fixture','node-fixture'),not_before='2026-11-21T12:00:00.000Z',
        stop_at='2026-11-21T14:00:00.000Z',human_window_stop_at='2026-11-21T14:05:00.000Z',
        human_window_sha256='1'*64,required_seconds=600,final_readback_seconds=60,cadence_seconds=30,
        max_gap_seconds=90,max_clock_drift_seconds=2,max_samples=2048,max_bytes=2*1048576,
        bindings=dict(agent_source_sha256='2'*64,config_sha256='3'*64,
            api_producer=dict(artifact_sha256='4'*64,instance_sha256='5'*64),
            ingress_producer=dict(artifact_sha256='6'*64,instance_sha256='7'*64),
            central_reader_sha256='8'*64,clock_evidence_sha256='9'*64,central_gates_sha256='0'*64,
            deployment_preflight_sha256='a'*64))


def event(seconds=0):
    now=datetime(2026,11,21,13,tzinfo=timezone.utc)+timedelta(seconds=seconds)
    return dict(started_at=stamp(now-timedelta(seconds=1)),at=stamp(now),
        monotonic_start_ns=(seconds+1)*10**9,monotonic_end_ns=(seconds+2)*10**9,
        boot_id='11111111-1111-1111-1111-111111111111',writer_sha256='a'*64,
        ntp_synchronized=True,transport_succeeded=True,request_sent_at=stamp(now-timedelta(seconds=1)),
        local=dict(auth_mode='mtls',mtls_state='SHADOW_ACTIVE',retry_pending=False,health_healthy=True,
            agent_source_sha256='2'*64,config_sha256='3'*64),
        receipt=dict(schema_version=1,kind='mtls_heartbeat_commit',received_at=stamp(now),
            request_sent_at=stamp(now-timedelta(seconds=1)),node_sha256=manifest()['node_sha256'],
            credential_sha256='b'*64,certificate_fingerprint_sha256='c'*64,
            credential_not_before='2026-11-20T00:00:00.000Z',credential_expires_at='2026-11-22T00:00:00.000Z',
            credential_acknowledged_at='2026-11-20T01:00:00.000Z',api_producer=manifest()['bindings']['api_producer'],
            ingress_producer=manifest()['bindings']['ingress_producer']))


class ManagedObservationDecisions(unittest.TestCase):
    def sample(self,e=None,previous=None):
        return make_sample(manifest(),'d'*64,e or event(),previous,digest(previous) if previous else None,'e'*64)

    def test_actual_correlated_cycles_form_one_segment_with_no_invented_samples(self):
        first=self.sample();second=self.sample(event(30),first)
        self.assertEqual((first['sequence'],second['sequence']),(1,2))
        self.assertEqual((first['segment'],second['segment']),(1,1))
        self.assertEqual(second['result'],'QUALIFYING')
        self.assertEqual(second['previous_sha256'],digest(first))

    def test_lost_reply_records_a_gap_and_never_joins_failed_interval(self):
        first=self.sample();lost=event(30);lost.update(receipt=None,transport_succeeded=False)
        gap=self.sample(lost,first);recovered=self.sample(event(60),gap)
        self.assertEqual(gap['result'],'GAP')
        self.assertIn('TRANSPORT_FAILED',gap['reasons'])
        self.assertIn('RECEIPT_MISSING',gap['reasons'])
        self.assertGreater(recovered['segment'],first['segment'])
        self.assertEqual(recovered['segment_start_sequence'],3)

    def test_source_config_producer_and_node_drift_block_positive_evidence(self):
        for key in ('source','config','api','ingress','node'):
            e=event()
            if key in {'source','config'}:
                e['local']['agent_source_sha256' if key=='source' else 'config_sha256']='0'*64
            elif key=='node':e['receipt']['node_sha256']='0'*64
            else:e['receipt'][key+'_producer']['instance_sha256']='0'*64
            with self.subTest(key=key):
                self.assertEqual(self.sample(e)['result'],'GAP')

    def test_bearer_retry_unready_and_local_health_failure_are_gaps(self):
        for change in [{'auth_mode':'bootstrap-mtls'},{'mtls_state':'SHADOW_READY'},
                {'retry_pending':True},{'health_healthy':False}]:
            e=event();e['local'].update(change)
            self.assertIn('LOCAL_NOT_READY',self.sample(e)['reasons'])

    def test_clock_discontinuity_and_missed_cadence_are_retained(self):
        first=self.sample();late=self.sample(event(91),first)
        self.assertIn('CADENCE_GAP',late['reasons'])
        jumped=event(30);jumped['monotonic_end_ns']+=10*10**9
        self.assertIn('CLOCK_DISCONTINUITY',self.sample(jumped,first)['reasons'])
        unsynchronized=event();unsynchronized['ntp_synchronized']=False
        self.assertIn('CLOCK_UNVERIFIED',self.sample(unsynchronized)['reasons'])

    def test_boot_or_writer_restart_cannot_stitch_a_long_segment(self):
        first=self.sample()
        for changes in [{'boot_id':'22222222-2222-2222-2222-222222222222'},{'writer_sha256':'0'*64}]:
            e=event(30);e.update(changes)
            self.assertIn('BOOT_OR_WRITER_CHANGED',self.sample(e,first)['reasons'])

    def test_central_heartbeat_must_advance_and_rotation_needs_acknowledged_overlap(self):
        first=self.sample();stale=event(30);stale['receipt']=copy.deepcopy(first['receipt'])
        self.assertIn('CENTRAL_TIME_NOT_ADVANCED',self.sample(stale,first)['reasons'])
        rotated=event(30);rotated['receipt']['credential_sha256']='e'*64
        rotated['receipt']['credential_acknowledged_at']='2026-11-21T13:00:10.000Z'
        self.assertEqual(self.sample(rotated,first)['result'],'QUALIFYING')
        old=copy.deepcopy(first);old['receipt']['credential_expires_at']='2026-11-21T13:00:05.000Z'
        self.assertIn('CREDENTIAL_CONTINUITY_UNPROVEN',self.sample(rotated,old)['reasons'])

    def test_short_window_cannot_claim_installed_24h_or_extend_human_deadline(self):
        for change in [{'scope':'installed_node'},{'stop_at':'2026-11-21T15:00:00.000Z'},
                {'required_seconds':7201},{'max_gap_seconds':True},{'extra':'PRIVATE_TEST_SENTINEL'}]:
            with self.subTest(change=change),self.assertRaises((ObservationError,ValueError)):
                validate_manifest({**manifest(),**change})


class IndependentManagedQualification(unittest.TestCase):
    def fixture(self):
        m=manifest();samples=[];previous=None
        for seconds in range(0,601,30):
            previous=make_sample(m,'d'*64,event(seconds),previous,digest(previous) if previous else None,'e'*64)
            samples.append(previous)
        receipt=samples[-1]['receipt'];now=datetime(2026,11,21,13,10,1,tzinfo=timezone.utc)
        central=dict(schema_version=1,kind='MANAGED_MTLS_OBSERVATION_CENTRAL_READBACK_V1',manifest_sha256='d'*64,
            deployment_preflight_sha256=m['bindings']['deployment_preflight_sha256'],node_sha256=m['node_sha256'],
            observed_at=stamp(now),database_at=stamp(now),reader_artifact_sha256=m['bindings']['central_reader_sha256'],
            configured_api_producer=m['bindings']['api_producer'],effective_gates_sha256=m['bindings']['central_gates_sha256'],
            transaction_read_only=True,source_provenance='CONFIGURED_BINDING_REQUIRES_EXECUTOR_ADMISSION',
            last_heartbeat_at=receipt['received_at'],active_credential_sha256=receipt['credential_sha256'],
            active_certificate_fingerprint_sha256=receipt['certificate_fingerprint_sha256'],node_active=True,node_status='ACTIVE',
            reconciliation_status='IN_SYNC',desired_version=1,observed_version=1,unresolved_lifecycle=False,
            credentials=[dict(credential_sha256=receipt['credential_sha256'],certificate_fingerprint_sha256=receipt['certificate_fingerprint_sha256'],
                not_before=receipt['credential_not_before'],expires_at=receipt['credential_expires_at'],acknowledged_at=receipt['credential_acknowledged_at'],
                revoked_at=None,issuer_revoked_at=None,previous_valid_until=None,status='ACTIVE',replacement_of_sha256=None)])
        local=dict(kind='MANAGED_MTLS_OBSERVATION_LOCAL_READBACK_V1',observed_at=stamp(now),node_sha256=m['node_sha256'],
            agent_source_sha256=m['bindings']['agent_source_sha256'],config_sha256=m['bindings']['config_sha256'],
            writer_sha256=samples[-1]['writer_sha256'],boot_id=samples[-1]['boot_id'],ntp_synchronized=True,service_active=True,
            primary_mtls=True,mtls_state='SHADOW_ACTIVE',retry_pending=False,pending_acknowledgement=False,
            certificate_fingerprint_sha256=receipt['certificate_fingerprint_sha256'])
        return m,samples,central,local,now

    def test_single_actual_duration_contract_is_synthetic_only_and_never_fleet_acceptance(self):
        m,s,c,l,now=self.fixture();result=qualify_interval(m,'d'*64,s,c,l,now)
        self.assertEqual(result['qualified_seconds'],600)
        self.assertEqual(result['result'],'PASS_SYNTHETIC_MECHANISM_ONLY')
        self.assertFalse(result['measured_24h'] or result['physical_vpn_accepted'] or result['fleet_accepted'] or result['full_beta_accepted'])

    def test_generic_pass_replacement_unknown_source_and_advanced_central_timestamp_refuse(self):
        for change in [{'pass':True},{'reader_artifact_sha256':'0'*64},{'effective_gates_sha256':'f'*64},
                {'last_heartbeat_at':'2026-11-21T13:10:00.001Z'},{'unresolved_lifecycle':True},
                {'configured_api_producer':{'artifact_sha256':'f'*64,'instance_sha256':'f'*64}}]:
            m,s,c,l,now=self.fixture()
            with self.subTest(change=change),self.assertRaises(ObservationError):
                qualify_interval(m,'d'*64,s,{**c,**change},l,now)

    def test_two_short_segments_are_never_added_to_make_required_interval(self):
        m,s,c,l,now=self.fixture()
        # A restart at the middle creates a retained break; both sides are short.
        values=[];previous=None
        for seconds in range(0,601,30):
            e=event(seconds)
            if seconds>=300:e['writer_sha256']='0'*64
            previous=make_sample(m,'d'*64,e,previous,digest(previous) if previous else None,'e'*64)
            values.append(previous)
        with self.assertRaises(ObservationError):qualify_interval(m,'d'*64,values,c,l,now)

    def test_fresh_independent_local_state_and_exact_valid_credential_are_required(self):
        for kind in ['local-cert','source','service','pending-ack','clock','revoked','expired']:
            m,s,c,l,now=self.fixture()
            if kind=='local-cert':l['certificate_fingerprint_sha256']='0'*64
            if kind=='source':l['agent_source_sha256']='0'*64
            if kind=='service':l['service_active']=False
            if kind=='pending-ack':l['pending_acknowledgement']=True
            if kind=='clock':l['ntp_synchronized']=False
            if kind=='revoked':c['credentials'][0]['issuer_revoked_at']='2026-11-21T13:05:00.000Z'
            if kind=='expired':c['credentials'][0]['expires_at']='2026-11-21T13:05:00.000Z'
            with self.subTest(kind=kind),self.assertRaises(ObservationError):qualify_interval(m,'d'*64,s,c,l,now)

    def test_stale_readback_and_missing_real_time_duration_cannot_qualify(self):
        m,s,c,l,now=self.fixture()
        with self.assertRaises(ObservationError):qualify_interval(m,'d'*64,s,c,l,now+timedelta(seconds=91))
        with self.assertRaises(ObservationError):qualify_interval(m,'d'*64,s[:-1],c,l,now)

    def test_normal_rotation_preserves_only_proven_historical_validity(self):
        m,_,central,local,_=self.fixture()
        start=datetime.fromisoformat(event()['at'])
        day=86400
        switch=day-300
        def after(seconds):
            return stamp(start+timedelta(seconds=seconds))
        now=start+timedelta(seconds=day+1)
        m.update(required_seconds=day,cadence_seconds=60,not_before=after(-60),stop_at=after(day+60),
                 human_window_stop_at=after(day+240),final_readback_seconds=120,max_bytes=16*1048576)
        central.update(last_heartbeat_at=after(day),observed_at=stamp(now),database_at=stamp(now))
        local['observed_at']=stamp(now)
        old=central['credentials'][0]
        new=dict(old,credential_sha256='e'*64,certificate_fingerprint_sha256='f'*64,
                 replacement_of_sha256=old['credential_sha256'],previous_valid_until=after(switch+30),
                 not_before=after(switch-60),acknowledged_at=after(switch-5),expires_at=after(day*2))
        # Persistence records the overlap on the replacement and shortens the predecessor's expiry.
        old.update(expires_at=after(switch+30),status='EXPIRED')
        central['credentials'].append(new)
        central.update(active_credential_sha256=new['credential_sha256'],
                       active_certificate_fingerprint_sha256=new['certificate_fingerprint_sha256'])
        local['certificate_fingerprint_sha256']=new['certificate_fingerprint_sha256']
        samples=[]
        for seconds in range(0,day+1,60):
            e=event(seconds)
            e['receipt']['credential_expires_at']=after(day*2)
            if seconds>=switch:
                e['receipt'].update(credential_sha256=new['credential_sha256'],
                    certificate_fingerprint_sha256=new['certificate_fingerprint_sha256'],
                    credential_not_before=new['not_before'],credential_acknowledged_at=new['acknowledged_at'])
            previous=samples[-1] if samples else None
            samples.append(make_sample(m,'d'*64,e,previous,digest(previous) if previous else None,'e'*64))
        result=qualify_interval(m,'d'*64,samples,central,local,now)
        self.assertEqual(result['qualified_seconds'],day)
        self.assertFalse(result['measured_24h'])
        for position,change in [(0,{'expires_at':after(switch+29)}),(0,{'issuer_revoked_at':after(switch-60)}),
                                (1,{'replacement_of_sha256':'0'*64}),(1,{'previous_valid_until':after(switch-10)}),
                                (1,{'expires_at':after(switch+50)}),(1,{'revoked_at':after(switch+100)})]:
            altered=copy.deepcopy(central)
            altered['credentials'][position].update(change)
            with self.subTest(change=change),self.assertRaises(ObservationError):
                qualify_interval(m,'d'*64,samples,altered,local,now)


if __name__=='__main__':
    unittest.main(verbosity=2)
