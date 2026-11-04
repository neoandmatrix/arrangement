package ai.bandobast.bandobast

import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodChannel
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.os.Handler
import android.os.Build
import android.os.Looper
import android.os.PowerManager
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** Android-only PCM bridge: communication mic with hardware AEC, no extra native dependency. */
class MainActivity : FlutterActivity() {
    private val main = Handler(Looper.getMainLooper())
    private var events: EventChannel.EventSink? = null
    private var recorder: AudioRecord? = null
    private var player: AudioTrack? = null
    private var echo: AcousticEchoCanceler? = null
    private var noise: NoiseSuppressor? = null
    private var captureThread: Thread? = null
    private var playbackThread: Thread? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private val running = AtomicBoolean(false)
    private val flush = AtomicBoolean(false)
    private val playback = ArrayBlockingQueue<ByteArray>(80)
    @Volatile private var generation = 0

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        EventChannel(flutterEngine.dartExecutor.binaryMessenger, "bandobast/audio_input")
            .setStreamHandler(object : EventChannel.StreamHandler {
                override fun onListen(arguments: Any?, sink: EventChannel.EventSink) { events = sink }
                override fun onCancel(arguments: Any?) { events = null }
            })
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "bandobast/audio")
            .setMethodCallHandler { call, result ->
                try {
                    when (call.method) {
                        "start" -> {
                            startAudio(call.argument<Int>("outputRate") ?: 24000)
                            result.success(mapOf("echoCancellation" to (echo?.enabled == true)))
                        }
                        "play" -> {
                            val bytes = call.arguments as ByteArray
                            if (running.get() && !playback.offer(bytes)) {
                                // Do not let network bursts create seconds of stale call audio.
                                playback.clear()
                                flush.set(true)
                                playback.offer(bytes)
                            }
                            result.success(null)
                        }
                        "clear" -> { playback.clear(); flush.set(true); result.success(null) }
                        "speaker" -> {
                            routeAudio(call.arguments == true)
                            result.success(null)
                        }
                        "stop" -> { stopAudio(); result.success(null) }
                        else -> result.notImplemented()
                    }
                } catch (e: Exception) {
                    stopAudio()
                    result.error("audio", e.message ?: "Audio unavailable", null)
                }
            }
    }

    @Suppress("DEPRECATION", "MissingPermission")
    private fun startAudio(outputRate: Int) {
        require(outputRate == 16000 || outputRate == 24000) { "Unsupported audio rate" }
        stopAudio()
        val manager = getSystemService(AUDIO_SERVICE) as AudioManager
        manager.mode = AudioManager.MODE_IN_COMMUNICATION
        routeAudio(false)
        manager.requestAudioFocus(null, AudioManager.STREAM_VOICE_CALL, AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
        val inputSize = AudioRecord.getMinBufferSize(16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        require(inputSize > 0) { "Microphone does not support PCM16" }
        val input = AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, 16000,
            AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(inputSize * 2, 6400))
        recorder = input
        require(input.state == AudioRecord.STATE_INITIALIZED) { "Microphone initialization failed" }
        if (AcousticEchoCanceler.isAvailable()) echo = AcousticEchoCanceler.create(input.audioSessionId)?.also { it.setEnabled(true) }
        if (NoiseSuppressor.isAvailable()) noise = NoiseSuppressor.create(input.audioSessionId)?.also { it.setEnabled(true) }
        val outputSize = AudioTrack.getMinBufferSize(outputRate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
        require(outputSize > 0) { "Speaker does not support PCM16" }
        val output = AudioTrack.Builder()
            .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
            .setAudioFormat(AudioFormat.Builder().setSampleRate(outputRate).setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
            .setBufferSizeInBytes(maxOf(outputSize * 2, outputRate / 5)).setTransferMode(AudioTrack.MODE_STREAM).build()
        player = output
        require(output.state == AudioTrack.STATE_INITIALIZED) { "Speaker initialization failed" }
        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "bandobast:call").also { it.acquire(16 * 60 * 1000L) }
        running.set(true)
        val sessionGeneration = generation
        input.startRecording()
        require(input.recordingState == AudioRecord.RECORDSTATE_RECORDING) { "Microphone could not start" }
        output.play()
        captureThread = Thread({
            val buffer = ByteArray(640) // 20 ms at 16 kHz, mono PCM16
            try {
                while (running.get() && sessionGeneration == generation) {
                    val count = input.read(buffer, 0, buffer.size, AudioRecord.READ_BLOCKING)
                    if (count < 0) throw IllegalStateException("Microphone disconnected")
                    if (count > 0) {
                        val copy = buffer.copyOf(count)
                        main.post { if (running.get() && sessionGeneration == generation) events?.success(copy) }
                    }
                }
            } catch (e: Exception) {
                main.post { if (running.get() && sessionGeneration == generation) events?.error("audio", e.message, null) }
            }
        }, "bandobast-mic").also { it.start() }
        playbackThread = Thread({
            try {
                while (running.get() && sessionGeneration == generation) {
                    if (flush.getAndSet(false)) { output.pause(); output.flush(); output.play() }
                    val bytes = playback.poll(40, TimeUnit.MILLISECONDS) ?: continue
                    var offset = 0
                    while (offset < bytes.size && running.get() && sessionGeneration == generation && !flush.get()) {
                        val written = output.write(bytes, offset, bytes.size - offset, AudioTrack.WRITE_BLOCKING)
                        if (written <= 0) throw IllegalStateException("Audio playback disconnected")
                        offset += written
                    }
                }
            } catch (e: Exception) {
                main.post { if (running.get() && sessionGeneration == generation) events?.error("audio", e.message, null) }
            }
        }, "bandobast-speaker").also { it.start() }
    }

    @Suppress("DEPRECATION")
    private fun stopAudio() {
        running.set(false)
        generation++
        try { recorder?.stop() } catch (_: Exception) {}
        try { player?.pause(); player?.flush() } catch (_: Exception) {}
        captureThread?.join(300)
        playbackThread?.join(300)
        captureThread = null; playbackThread = null
        recorder?.release(); recorder = null
        player?.release(); player = null
        echo?.release(); echo = null
        noise?.release(); noise = null
        playback.clear(); flush.set(false)
        wakeLock?.let { if (it.isHeld) it.release() }; wakeLock = null
        val manager = getSystemService(AUDIO_SERVICE) as AudioManager
        routeAudio(false)
        manager.mode = AudioManager.MODE_NORMAL
        manager.abandonAudioFocus(null)
    }

    @Suppress("DEPRECATION")
    private fun routeAudio(speaker: Boolean) {
        val manager = getSystemService(AUDIO_SERVICE) as AudioManager
        if (Build.VERSION.SDK_INT >= 31) {
            if (speaker) {
                manager.availableCommunicationDevices.firstOrNull { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER }
                    ?.let { manager.setCommunicationDevice(it) }
            } else {
                // Let Android prefer wired/headset audio, otherwise the earpiece.
                manager.clearCommunicationDevice()
            }
        } else {
            manager.isSpeakerphoneOn = speaker
        }
    }

    override fun onDestroy() { stopAudio(); super.onDestroy() }
}
