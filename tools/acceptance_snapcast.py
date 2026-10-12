"""Opt-in Linux same-host mpv/Snapcast digital acceptance (no installation).

Direct FIFO standalone mode uses only Python's standard library. --orr-ipc adds
production ORR HTTP/Unix JSON IPC and defaults to an isolated clocked PulseAudio
null sink/monitor ingress; install project dependencies and official PulseAudio
utilities first. --ingress=fifo preserves the direct-PCM diagnostic path. Both modes use
quiet synthetic stereo, a private FIFO, loopback listeners, synthetic client IDs,
and official file outputs. Never a physical device, vendor account, or DRM key.
No two-host clock-skew, real speaker latency, or audible-output claim.

Keep output private: raw samples and upstream logs are local diagnostics.
"""
from __future__ import annotations
import asyncio
from collections import deque
from array import array
import json
import math
import os
from pathlib import Path
import secrets
import statistics
import argparse
import shutil
import socket
import struct
import subprocess
import sys
import time
import wave

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from tools.native_probe_supervisor import run_probe

RATE = 48000


def generate(path: Path) -> None:
    second = b''.join(struct.pack('<hh', *[round(500 * math.sin(2 * math.pi * hz * i / RATE))
                                         for hz in (440, 733)]) for i in range(RATE))
    with wave.open(str(path), 'wb') as output:
        output.setparams((2, 2, RATE, 0, 'NONE', 'not compressed'))
        for _ in range(90):
            output.writeframesraw(second)


def freeport() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def simplified(server: dict) -> dict:
    return {'groups': [{'id': group['id'], 'muted': group['muted'],
                        'stream': group['stream_id'], 'clients': [
                            {'id': c['id'], 'connected': c['connected'], 'volume': c['config']['volume']}
                            for c in group['clients']]} for group in server['groups']],
            'streams': [{'id': s['id'], 'status': s['status']} for s in server['streams']]}


def assert_owned_status(state, stream_id, client_ids):
    """Reject a recycled port or unexpected peer before sending any mutation."""
    assert {stream['id'] for stream in state['streams']} == {stream_id}, 'Unexpected stream identity'
    members = [client['id'] for group in state['groups'] for client in group['clients']]
    assert len(members) == len(set(members)) and set(members) <= set(client_ids), 'Unexpected client identity'
    assert all(group['stream_id'] == stream_id for group in state['groups']), 'Unexpected group stream'


def assert_measurement_state(state, stream_id, client_ids, groups, percent, source_status='playing'):
    assert_owned_status(state, stream_id, client_ids)
    expected_source_states = (source_status,) if isinstance(source_status, str) else source_status
    assert state['streams'][0]['status'] in expected_source_states, 'Unexpected source state'
    actual_groups = {frozenset(client['id'] for client in group['clients']): group['muted']
                     for group in state['groups']}
    assert len(actual_groups) == len(state['groups']) and actual_groups == groups, 'Unexpected group state'
    clients = {client['id']: client for group in state['groups'] for client in group['clients']}
    assert set(clients) == set(client_ids), 'Missing measured client'
    assert all(client['connected'] for client in clients.values()), 'Measured client disconnected'
    assert all(client['config']['volume'] == {'muted': False, 'percent': percent}
               for client in clients.values()), 'Unexpected retained client volume'


def assert_reconnected_status(state, stream_id, client_ids, group_id, percent):
    assert len(state['groups']) == 1 and state['groups'][0]['id'] == group_id, 'Reconnect changed original group'
    assert_measurement_state(state, stream_id, client_ids, {frozenset(client_ids): False}, percent)


def tone_amplitudes(samples, frequency):
    """Fit independent 100 ms tone windows, allowing clock-correction phase jumps.

    A single whole-capture projection assumes phase continuity, which Snapclient
    does not promise while correcting its clock. Least-squares sin/cos fits also
    avoid incomplete-cycle bias for the 733 Hz channel. RMS still covers every
    sample; acceptance requires at least 80% of complete windows at target gain.
    """
    count = RATE // 10
    cosine = [math.cos(2 * math.pi * frequency * i / RATE) for i in range(count)]
    sine = [math.sin(2 * math.pi * frequency * i / RATE) for i in range(count)]
    cc, ss = sum(value * value for value in cosine), sum(value * value for value in sine)
    cs = sum(c * s for c, s in zip(cosine, sine))
    determinant = cc * ss - cs * cs
    result = []
    for start in range(0, len(samples) - count + 1, count):
        window = samples[start:start + count]
        xc = sum(value * c for value, c in zip(window, cosine))
        xs = sum(value * v for value, v in zip(window, sine))
        real, imag = (xc * ss - xs * cs) / determinant, (xs * cc - xc * cs) / determinant
        result.append(math.hypot(real, imag))
    assert len(result) >= 5, 'Too few complete tone windows'
    return result


def ipc_summary(args, response, elapsed):
    """Keep only fixed command/property labels, never media/response contents."""
    name = args.get('name') if isinstance(args, dict) else (args[0] if args else None)
    label = name if name in {'get_property', 'set_property', 'loadfile', 'stop', 'seek', 'af'} else 'other'
    properties = {'idle-active', 'pause', 'playback-time', 'duration', 'media-title', 'filename',
                  'path', 'playlist-path', 'playlist', 'stream-open-filename', 'core-idle',
                  'eof-reached', 'paused-for-cache', 'cache-buffering-state', 'demuxer-cache-state',
                  'track-list', 'mpv-version', 'current-ao', 'volume', 'mute', 'af'}
    if label in {'get_property', 'set_property'}:
        prop = args.get('name') if isinstance(args, dict) else (args[1] if len(args) > 1 else None)
        label += ':' + (prop if prop in properties else 'other')
    error = response.get('error')
    outcome = 'success' if error == 'success' else ('timeout' if 'timed out' in str(error) else 'rejected')
    return {'command': label, 'seconds': round(elapsed, 4), 'outcome': outcome}


def assert_upstream_active(snapshot, mute, expected_mute, source):
    """Silent PCM cannot substitute for actual active, acknowledged mpv state."""
    assert snapshot.get('backend-error') is not True, 'Native playback state unavailable'
    assert snapshot.get('path') == str(source), 'Native source changed'
    assert snapshot.get('idle-active') is False and snapshot.get('pause') is False, 'Native source not active'
    assert snapshot.get('eof-reached') is False and snapshot.get('paused-for-cache') is False, 'Native source stalled'
    assert mute is expected_mute, 'Native mute changed'
    position = snapshot.get('playback-time')
    assert type(position) in (int, float) and math.isfinite(position), 'Native position unavailable'
    return position


async def exercise(directory: Path, args: argparse.Namespace) -> dict:

    args.ingress = getattr(args, 'ingress', None) or ('pulse' if args.orr_ipc else 'fifo')
    run_id = secrets.token_hex(16)
    stream_id = f'orr-digital-{run_id}'
    client_ids = [f'orr-private-{run_id}-{suffix}' for suffix in ('a', 'b')]
    directory.mkdir(parents=True, mode=0o700)
    tone = directory / 'quiet-stereo.wav'
    generate(tone)
    fifo = directory / 'source.fifo'
    os.mkfifo(fifo, 0o600)
    stream_port, rpc_port, http_port = freeport(), freeport(), freeport()
    assert len({stream_port, rpc_port, http_port}) == 3
    config = directory / 'snapserver.conf'
    config.write_text(f'''[server]
datadir = {directory}/snapserver-state
threads = 2
[http]
enabled = false
[tcp]
enabled = true
bind_to_address = 127.0.0.1
port = {rpc_port}
[stream]
bind_to_address = 127.0.0.1
port = {stream_port}
source = pipe://{fifo}?name={stream_id}&mode=read
sampleformat = 48000:16:2
codec = pcm
buffer = 200
chunk_ms = 20
send_to_muted = true
[logging]
sink = stdout
filter = *:info
''')
    children, logs = [], []
    processes = {}
    phase = 'setup'
    ipc_commands = deque(maxlen=12)
    ipc_failures = deque(maxlen=12)
    server_identity_verified = False
    backend, runner, session = None, None, None
    evidence = {'lane': 'REAL_BACKEND/SAME_HOST_SNAPCAST_DIGITAL',
                'source': 'synthetic 48 kHz signed-16-bit stereo, 440/733 Hz, peak 500',
                'transport': 'official mpv CLI -> PCM FIFO -> Snapserver -> two file-output Snapclients',
                'ingress': args.ingress,
                'production_unix_ipc': 'NOT_RUN',
                'hardware_and_two_host_synchronization': 'NOT TESTED',
                'non_atomic_group_gain': True, 'stages': {}, 'ports': [stream_port, rpc_port, http_port]}
    request_id = 0

    # Prevent packaged Avahi support from registering on a real system bus.
    # This only narrows each child's environment; no service/settings change.
    child_env = os.environ.copy()
    child_env['DBUS_SYSTEM_BUS_ADDRESS'] = f'unix:path={directory}/unused-dbus'
    child_env['DBUS_SESSION_BUS_ADDRESS'] = child_env['DBUS_SYSTEM_BUS_ADDRESS']

    def launch(name, command_args, *, output=None):
        log = (directory / f'{name}.log').open('w')
        logs.append(log)
        child = subprocess.Popen(command_args, stdin=subprocess.DEVNULL, stdout=output or log, stderr=log, env=child_env)
        children.append(child)
        processes[name] = child
        return child

    def launch_client(index, suffix=''):
        return launch(f'client-{index}{suffix}', [args.snapclient, '--host', '127.0.0.1',
                      '--port', str(stream_port), '--hostID', client_ids[index],
                      '--player', f'file:filename={directory}/client-{index}.pcm,mode=a',
                      '--mixer', 'software:poly:1', '--logsink', 'stdout'])

    async def rpc(method, params=None):
        nonlocal server_identity_verified
        assert server.poll() is None, 'Owned Snapserver exited'
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_connection('127.0.0.1', rpc_port)
            async def exchange(request_method, request_params=None):
                nonlocal request_id
                request_id += 1
                rid = request_id
                body = {'id': rid, 'jsonrpc': '2.0', 'method': request_method}
                if request_params is not None:
                    body['params'] = request_params
                writer.write(json.dumps(body).encode() + b'\r\n')
                await writer.drain()
                while True:
                    raw = await reader.readline()
                    if not raw:
                        raise AssertionError('RPC EOF before acknowledged response')
                    reply = json.loads(raw)
                    if reply.get('id') == rid:
                        assert 'error' not in reply, (request_method, reply)
                        return reply['result']
            try:
                # Read-only identity verification uses the SAME connection as
                # the mutation, so released/reused ephemeral ports cannot steer
                # controls into a foreign Snapserver after an ownership check.
                state = (await exchange('Server.GetStatus'))['server']
                assert_owned_status(state, stream_id, client_ids)
                server_identity_verified = True
                assert server.poll() is None, 'Owned Snapserver exited'
                if method == 'Server.GetStatus':
                    return {'server': state}
                return await exchange(method, params)
            finally:
                writer.close()
                await writer.wait_closed()

    async def status():
        return (await rpc('Server.GetStatus'))['server']

    async def eventually(description, check, timeout=5):
        end = time.monotonic() + timeout
        last = None
        while time.monotonic() < end:
            try:
                last = await check()
                if last:
                    return last
            except (ConnectionRefusedError, OSError) as exc:
                last = str(exc)
            await asyncio.sleep(.05)
        raise AssertionError((description, last))

    async def connected(expected):
        state = await status()
        members = {c['id'] for g in state['groups'] for c in g['clients'] if c['connected']}
        return state if members == expected else None

    async def command(value):
        nonlocal phase
        phase = ':'.join(value.split(':')[:2])
        async with session.get(f'http://127.0.0.1:{http_port}/httpapi.asp', params={'command': value}) as response:
            body = await response.text()
            assert response.status == 200, (value, response.status, body)
            return json.loads(body) if response.content_type == 'application/json' else body

    async def measure(label, gains, *, groups, percent, settle=.8, source_status='playing', upstream_mute=None, upstream_stopped=False):
        nonlocal phase
        phase = label
        async def checked_state():
            assert all(child.poll() is None for child in processes.values()), 'Owned audio process exited'
            state = await status()
            assert_measurement_state(state, stream_id, client_ids, groups, percent, source_status)
            if upstream_stopped:
                assert await backend.get_property('idle-active') is True, 'Native player restarted during stop capture'
            return state
        async def native_position():
            snapshot = await backend.snapshot()
            mute = (await backend.checked_command(['get_property', 'mute'])).get('data')
            return assert_upstream_active(snapshot, mute, upstream_mute, tone)
        await asyncio.sleep(settle)
        await checked_state()
        native_before = await native_position() if upstream_mute is not None else None
        paths = [directory / f'client-{i}.pcm' for i in range(2)]
        offsets = [path.stat().st_size for path in paths]
        assert all(offset % 4 == 0 for offset in offsets)
        await asyncio.sleep(.7)
        result = []
        for i, (path, offset, gain) in enumerate(zip(paths, offsets, gains)):
            with path.open('rb') as pcm:
                pcm.seek(offset)
                raw = pcm.read()
            samples = array('h')
            samples.frombytes(raw)
            assert len(samples) >= RATE, (label, i, len(samples))
            channels = []
            for channel, hz in enumerate((440, 733)):
                x = samples[channel::2]
                rms = math.sqrt(sum(v*v for v in x) / len(x))
                peak = max(map(abs, x))
                zeros = sum(v == 0 for v in x)
                amplitudes = tone_amplitudes(x, hz)
                amplitude = statistics.median(amplitudes)
                tolerance = max(1.8, gain * 7)
                matching = sum(abs(value - 500 * gain) < tolerance for value in amplitudes) / len(amplitudes)
                if gain == 0:
                    assert peak == 0 and zeros == len(x), (label, i, channel, peak)
                else:
                    assert abs(rms - 500/math.sqrt(2)*gain) < max(1.1, gain*5), (label, i, channel, rms, gain)
                    assert abs(amplitude - 500*gain) < tolerance and matching >= .8, (label, i, channel, amplitude, gain, matching)
                channels.append({'rms': rms, 'peak': peak, 'tone_amplitude': amplitude, 'tone_window_match_fraction': matching, 'nonzero': len(x)-zeros})
            result.append({'client': client_ids[i], 'frames': len(samples)//2, 'expected_gain': gain, 'channels': channels})
        native_after = await native_position() if upstream_mute is not None else None
        if native_before is not None:
            assert native_after > native_before + .3, 'Native source did not advance during PCM capture'
        evidence['stages'][label] = {'pcm': result, 'state': simplified(await checked_state()),
                                     'expected_source_status': source_status}
        if native_before is not None:
            evidence['stages'][label]['native_playback'] = {'mute': upstream_mute,
                'position_before': native_before, 'position_after': native_after}
        print(f'PASS {label}', flush=True)
        return result

    async def gain(percent):
        # Snapcast has no atomic Group.SetVolume method. Explicit fan-out.
        for cid in client_ids:
            result = await rpc('Client.SetVolume', {'id': cid, 'volume': {'muted': False, 'percent': percent}})
            assert result['volume'] == {'muted': False, 'percent': percent}
        state = await status()
        actual = {c['id']: c['config']['volume'] for g in state['groups'] for c in g['clients']}
        assert all(actual[cid] == {'muted': False, 'percent': percent} for cid in client_ids)

    async def clocked_ingress():
        """Own an isolated digital-only Pulse daemon, never a user audio server.

        mpv's PCM AO writes while holding an AO lock; a real-time FIFO reader
        can therefore starve native control/property requests. A clocked null
        sink decouples that lock from Snapcast's reader. No timeout is relaxed.
        """
        home, runtime, cfg = (directory / name for name in ('home', 'runtime', 'pulse-config'))
        for path in (home, runtime, cfg):
            path.mkdir(mode=0o700)
        endpoint = runtime / 'native'
        pulse_server = f'unix:{endpoint}'
        cookie = cfg / 'fixture-cookie'
        cookie.write_bytes(bytes(256))
        cookie.chmod(0o600)
        (cfg / 'daemon.conf').write_text('default-sample-rate = 48000\nalternate-sample-rate = 48000\n'
            'default-sample-format = s16le\nresample-method = trivial\navoid-resampling = no\n')
        (cfg / 'client.conf').write_text(f'autospawn = no\ndefault-server = {pulse_server}\n'
            f'enable-shm = no\ncookie-file = {cookie}\n')
        for key in tuple(child_env):
            if key.startswith('PULSE_'):
                child_env.pop(key)
        child_env.update(HOME=str(home), XDG_CONFIG_HOME=str(cfg), PULSE_SERVER=pulse_server,
                         PULSE_COOKIE=str(cookie), PULSE_RUNTIME_PATH=str(runtime),
                         PULSE_STATE_PATH=str(directory / 'pulse-state'),
                         PULSE_CLIENTCONFIG=str(cfg / 'client.conf'),
                         PULSE_CONFIG=str(cfg / 'daemon.conf'), LC_ALL='C')
        pulse = launch('pulse', [args.pulseaudio, '-n', '--daemonize=no', '--use-pid-file=no',
            '--exit-idle-time=-1', '--disable-shm=yes', '--realtime=no', '--high-priority=no',
            '--log-target=stderr', '--log-level=warning',
            f'--load=module-native-protocol-unix socket={endpoint} auth-anonymous=yes auth-cookie={cookie}',
            '--load=module-null-sink sink_name=orr_snapcast rate=48000 channels=2 format=s16le norewinds=true'])

        async def ready():
            assert pulse.poll() is None, 'Owned private PulseAudio exited'
            return endpoint.is_socket()
        await eventually('private digital PulseAudio ready', ready)
        # Read-only module/sink checks before any player/recorder is attached.
        # The explicit server path and empty (-n) daemon configuration prohibit
        # autospawn, inherited default outputs, and physical-device modules.
        async def inspect(*params):
            child = await asyncio.create_subprocess_exec(args.pactl, '--server', pulse_server, *params,
                env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                output, _ = await asyncio.wait_for(child.communicate(), 3)
                assert child.returncode == 0, 'Private PulseAudio inspection failed'
                return output.decode()
            finally:
                if child.returncode is None:
                    child.kill()
                    await asyncio.wait_for(child.wait(), 3)
        modules = await inspect('list', 'short', 'modules')
        assert {line.split('\t')[1] for line in modules.strip().splitlines()} == {
            'module-null-sink', 'module-native-protocol-unix'}, 'Unexpected private PulseAudio module'
        sinks = (await inspect('list', 'short', 'sinks')).strip().splitlines()
        assert len(sinks) == 1 and sinks[0].split('\t')[1] == 'orr_snapcast', 'Unexpected private audio sink'
        assert 's16le 2ch 48000Hz' in sinks[0], 'Unexpected private sink format'
        # Linux O_RDWR avoids a startup rendezvous with Snapserver's reconnecting
        # nonblocking reader. Only parec writes this private descriptor. If the
        # reader dies, the owned recorder is bounded by liveness/teardown and
        # the outer process-group deadline, rather than leaving open() hung.
        descriptor = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.set_blocking(descriptor, True)
            launch('recorder', [args.parec, '--server', pulse_server, '--device=orr_snapcast.monitor',
                '--raw', '--format=s16le', '--rate=48000', '--channels=2', '--latency-msec=20'], output=descriptor)
        finally:
            os.close(descriptor)
        evidence['clocked_ingress'] = {'sink': 'private null', 'rate': RATE, 'channels': 2,
            'sample_format': 's16le', 'rewinds': False, 'continuous_monitor': True,
            'hardware_devices': False}
        return ['--ao=pulse', f'--pulse-host={pulse_server}', '--audio-device=pulse/orr_snapcast',
                '--pulse-buffer=50']

    async def failure_diagnostics():
        """Publish aggregate allowlisted state only; raw upstream logs stay private."""
        diagnostic = {'phase': phase, 'process_returncodes': {name: child.poll() for name, child in processes.items()},
                      'recent_ipc': list(ipc_commands), 'failed_ipc': list(ipc_failures), 'pcm': []}
        for index in range(2):
            path = directory / f'client-{index}.pcm'
            size = path.stat().st_size if path.exists() else 0
            samples = array('h')
            if size:
                with path.open('rb') as source:
                    source.seek(max(0, size - RATE))
                    raw = source.read(RATE)
                samples.frombytes(raw[:len(raw) // 2 * 2])
            diagnostic['pcm'].append({'bytes': size, 'tail_samples': len(samples),
                'tail_nonzero': sum(value != 0 for value in samples)})
        try:
            assert server_identity_verified, 'No owned server identity yet'
            async with asyncio.timeout(.5):
                state = await status()
                diagnostic['stream_states'] = [stream['status'] if stream['status'] in {'playing', 'idle', 'disabled'} else 'other' for stream in state['streams']]
                diagnostic['connected_clients'] = sum(client['connected'] for group in state['groups'] for client in group['clients'])
        except (Exception, asyncio.CancelledError):
            diagnostic['snapcast_status'] = 'unavailable'
        evidence['failure'] = diagnostic
        print('SNAPCAST_FAILURE ' + json.dumps(diagnostic, sort_keys=True), flush=True)

    try:
        phase = 'Snapserver startup'
        server = launch('server', [args.snapserver, '-c', str(config)])
        await eventually('Snapserver ready', status)
        clients = [launch_client(i) for i in range(2)]
        initial = await eventually('both real clients connected', lambda: connected(set(client_ids)))
        group_id = next(g['id'] for g in initial['groups'] if any(c['id'] == client_ids[0] for c in g['clients']))
        phase = 'audio ingress startup'
        output_args = await clocked_ingress() if args.ingress == 'pulse' else [
            '--ao=pcm', '--ao-pcm-waveheader=no', '--ao-pcm-append=yes', f'--ao-pcm-file={fifo}']
        mpv_args = [args.mpv, '--no-config', '--load-scripts=no', '--ytdl=no', '--no-video', '--volume=100',
                    '--audio-channels=stereo', '--audio-samplerate=48000', '--audio-format=s16',
                    *output_args]
        if args.orr_ipc:
            from aiohttp import ClientSession, ClientTimeout, web
            from fake_linkplay.config import Config
            from fake_linkplay.http_api import HttpApi
            from fake_linkplay.mpv_backend import MpvBackend
            from fake_linkplay.state import RendererState
            ipc = directory / 'mpv.sock'
            mpv = launch('mpv', [*mpv_args, '--idle=yes', f'--input-ipc-server={ipc}'])
            class ObservedMpvBackend(MpvBackend):
                async def command(self, args, timeout=5.0):
                    started = time.monotonic()
                    response = await super().command(args, timeout=timeout)
                    summary = ipc_summary(args, response, time.monotonic() - started)
                    ipc_commands.append(summary)
                    if summary['outcome'] != 'success':
                        ipc_failures.append(summary)
                    return response
            backend = ObservedMpvBackend(str(ipc), auto_spawn=False, initial_volume=100)
            await eventually('native mpv IPC readiness', lambda: backend.get_property('mpv-version'))
            cfg = Config(uuid='00000000-0000-4000-8000-000000000000', advertise_ip='127.0.0.1',
                         bind_address='127.0.0.1', http_api_port=http_port, upnp_port=http_port,
                         state_dir=str(directory/'orr-state'), auto_spawn_mpv=False,
                         enable_ssdp=False, enable_mdns=False, default_volume=100)
            api = HttpApi(cfg, RendererState.from_config(cfg), backend)
            api.queue_is_live = False
            runner = web.AppRunner(api.app(), shutdown_timeout=1)
            await runner.setup()
            await web.TCPSite(runner, '127.0.0.1', http_port).start()
            session = ClientSession(timeout=ClientTimeout(total=10), trust_env=False)
            assert await command(f'setPlayerCmd:play:{tone}') == 'OK'
            player = await command('getPlayerStatus')
            assert player['status'] == 'play' and player['transport_error'] == '', player
            assert await backend.get_property('current-ao') == ('pulse' if args.ingress == 'pulse' else 'pcm')
            evidence['production_unix_ipc'] = 'HTTP play/readiness acknowledged'
            evidence['transport'] = ('ORR HttpApi -> production MpvBackend Unix JSON IPC -> mpv -> '
                + ('private Pulse null monitor -> PCM FIFO' if args.ingress == 'pulse' else 'PCM FIFO')
                + ' -> Snapcast file outputs')
        else:
            mpv = launch('mpv', [*mpv_args, str(tone)])
        evidence['versions'] = {name: subprocess.check_output([getattr(args, name), '--version'],
                                text=True, timeout=3, env=child_env, stderr=subprocess.DEVNULL).splitlines()[0]
                                for name in (('snapserver', 'snapclient', 'mpv', 'pulseaudio') if args.ingress == 'pulse'
                                             else ('snapserver', 'snapclient', 'mpv'))}
        separate = {frozenset([cid]): False for cid in client_ids}
        joined_active = {frozenset(client_ids): False}
        joined_muted = {frozenset(client_ids): True}
        await measure('separate_clients_receive_source', [1, 1], groups=separate, percent=100, settle=1.3)
        result = await rpc('Group.SetClients', {'id': group_id, 'clients': client_ids})
        joined = result['server']
        assert len(joined['groups']) == 1 and {c['id'] for c in joined['groups'][0]['clients']} == set(client_ids)
        await gain(50)
        await measure('joined_group_gain_50', [.5, .5], groups=joined_active, percent=50)
        assert (await rpc('Group.SetMute', {'id': group_id, 'mute': True}))['mute'] is True
        await measure('joined_group_muted', [0, 0], groups=joined_muted, percent=50)
        await rpc('Group.SetMute', {'id': group_id, 'mute': False})
        await rpc('Group.SetClients', {'id': group_id, 'clients': [client_ids[0]]})
        state = await status()
        assert len(state['groups']) == 2 and all(len(g['clients']) == 1 for g in state['groups'])
        await rpc('Group.SetMute', {'id': group_id, 'mute': True})
        await measure('leave_member_isolated_from_group_mute', [0, .5],
                      groups={frozenset([client_ids[0]]): True, frozenset([client_ids[1]]): False}, percent=50)
        await rpc('Group.SetMute', {'id': group_id, 'mute': False})
        await rpc('Group.SetClients', {'id': group_id, 'clients': client_ids})
        await measure('rejoined_both_active', [.5, .5], groups=joined_active, percent=50)
        clients[1].terminate()
        clients[1].wait(timeout=3)
        processes.pop('client-1')
        disconnected = await eventually('member disconnect reflected', lambda: connected({client_ids[0]}))
        evidence['member_disconnect'] = simplified(disconnected)
        clients[1] = launch_client(1, '-reconnect')
        restored = await eventually('member reconnect retains group', lambda: connected(set(client_ids)))
        assert_reconnected_status(restored, stream_id, client_ids, group_id, 50)
        await measure('same_id_reconnect_preserves_group_gain', [.5, .5], groups=joined_active, percent=50, settle=1.2)
        await gain(100)
        await measure('restored_group_gain_100', [1, 1], groups=joined_active, percent=100)
        if args.orr_ipc:
            assert await command('setPlayerCmd:vol:50') == 'OK'
            assert abs(await backend.get_property('volume') - 50) < .001
            await measure('orr_http_upstream_gain_50', [.125, .125], groups=joined_active, percent=100, settle=1.3, upstream_mute=False)
            assert await command('setPlayerCmd:mute:1') == 'OK'
            assert await backend.get_property('mute') is True
            # Snapcast 0.27 tracks incoming bytes; 0.31 also classifies digital
            # silence as idle. Native active/mute/position checks above and
            # after capture distinguish intended silence from a stopped player.
            await measure('orr_http_upstream_mute', [0, 0], groups=joined_active, percent=100,
                          settle=1.3, source_status=('playing', 'idle'), upstream_mute=True)
            assert await command('setPlayerCmd:mute:0') == 'OK'
            assert await command('setPlayerCmd:vol:100') == 'OK'
            await measure('orr_http_upstream_restore', [1, 1], groups=joined_active, percent=100, settle=1.3, upstream_mute=False)
            assert await command('setPlayerCmd:stop') == 'OK'
            assert (await command('getPlayerStatus'))['status'] == 'stop'
            assert await backend.get_property('idle-active') is True
            await measure('orr_http_stop_drains_to_silence', [0, 0], groups=joined_active, percent=100, settle=1.5, source_status=('playing', 'idle') if args.ingress == 'pulse' else 'idle', upstream_stopped=True)
            evidence['production_unix_ipc'] = 'PASS: HTTP play/readiness, gain, mute, restore, stop with measured downstream PCM'
        assert all(child.poll() is None for child in processes.values())
        if args.ingress == 'pulse' and not args.orr_ipc:
            evidence['transport'] = 'official mpv CLI -> private Pulse null monitor -> PCM FIFO -> Snapcast file outputs'
        evidence['success'] = True
        return evidence
    except BaseException:
        await failure_diagnostics()
        raise
    finally:
        try:
            if session is not None:
                await session.close()
            if runner is not None:
                await runner.cleanup()
            if backend is not None:
                await backend.stop_process()
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
            for child in reversed(children):
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
        for log in logs:
            log.close()
        evidence['cleanup'] = {'all_children_reaped': all(child.poll() is not None for child in children),
                               'child_returncodes': [child.returncode for child in children]}
        for port in (stream_port, rpc_port, http_port):
            with socket.socket() as probe:
                probe.settimeout(.2)
                assert probe.connect_ex(('127.0.0.1', port)) != 0, ('listener leaked', port)
        evidence['cleanup']['owned_ports_closed'] = True
        (directory/'evidence.json').write_text(json.dumps(evidence, indent=2)+'\n')
        if fifo.exists():
            fifo.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapserver', default=shutil.which('snapserver'))
    parser.add_argument('--snapclient', default=shutil.which('snapclient'))
    parser.add_argument('--mpv', default=shutil.which('mpv'))
    parser.add_argument('--ingress', choices=('fifo', 'pulse'),
                        help='Audio ingress: defaults to clocked pulse with --orr-ipc, otherwise direct fifo')
    for executable in ('pulseaudio', 'parec', 'pactl'):
        parser.add_argument('--' + executable, default=shutil.which(executable))
    parser.add_argument('--output', type=Path, required=True,
                        help='New private evidence directory; existing paths are refused')
    parser.add_argument('--orr-ipc', action='store_true',
                        help='Also require real ORR HTTP/Unix JSON IPC. Unsupported on Unix-socket-restricted hosts.')
    parser.add_argument('--child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not __debug__:
        parser.error('Acceptance assertions require Python without -O')
    if sys.platform != 'linux' or sys.byteorder != 'little':
        parser.error('This measured lane currently supports little-endian Linux')
    args.ingress = args.ingress or ('pulse' if args.orr_ipc else 'fifo')
    required = ['snapserver', 'snapclient', 'mpv']
    if args.ingress == 'pulse':
        required += ['pulseaudio', 'parec', 'pactl']
    for name in required:
        value = getattr(args, name)
        if not value or not Path(value).is_file() or not os.access(value, os.X_OK):
            parser.error(f'{name} requires an existing official/reputable executable')
    args.output = args.output.resolve()
    if any(character in str(args.output) for character in '\r\n&?#,%='):
        parser.error('--output contains a reserved Snapcast URI/configuration character')
    if args.ingress == 'pulse' and any(character.isspace() for character in str(args.output)):
        parser.error('--ingress=pulse requires an output path without whitespace')
    if args.ingress == 'pulse' and len(os.fsencode(str(args.output / 'runtime' / 'native'))) >= 104:
        parser.error('--ingress=pulse needs a shorter output path for its private Unix socket')
    if args.orr_ipc and len(os.fsencode(str(args.output / 'mpv.sock'))) >= 104:
        parser.error('--orr-ipc needs a shorter output path for Unix socket portability')
    if args.output.exists():
        parser.error('--output must not already exist')
    if args.child:
        asyncio.run(exercise(args.output, args))
        return
    child_args = [sys.executable, str(Path(__file__).resolve()), '--child', '--output', str(args.output),
                  '--mpv', args.mpv, '--snapserver', args.snapserver, '--snapclient', args.snapclient]
    child_args += ['--ingress', args.ingress]
    if args.ingress == 'pulse':
        for executable in ('pulseaudio', 'parec', 'pactl'):
            child_args += ['--' + executable, getattr(args, executable)]
    if args.orr_ipc:
        child_args.append('--orr-ipc')
    try:
        code = run_probe(child_args, timeout=90, capture_output=False).returncode
    except subprocess.TimeoutExpired:
        raise SystemExit('FAIL: bounded acceptance timed out; owned process group killed')
    print('Private evidence:', args.output/'evidence.json')
    raise SystemExit(code)


if __name__ == '__main__':
    main()
