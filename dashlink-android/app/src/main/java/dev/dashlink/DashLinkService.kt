package dev.dashlink

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import fi.iki.elonen.NanoHTTPD
import java.security.SecureRandom

class DashLinkService : Service() {

    private lateinit var sensors: SensorHub
    private lateinit var location: LocationHub
    private lateinit var tts: TtsEngine
    private lateinit var stt: SttEngine
    private lateinit var whisper: WhisperEngine
    private lateinit var camera: CameraHub
    private lateinit var dashcam: Dashcam
    private lateinit var detectorEngine: DetectorEngine
    private lateinit var detector: Detector
    private var server: HttpServer? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        goForeground()

        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "DashLink::server")
            .apply { acquire() }

        sensors = SensorHub(this).also { it.start() }
        location = LocationHub(this).also { it.start() }
        tts = TtsEngine(this)
        stt = SttEngine(this)
        whisper = WhisperEngine(this)
        camera = CameraHub(this)
        dashcam = Dashcam(this, camera, sensors, location)
        val files = FileShare(getExternalFilesDir(null)!!)
        detectorEngine = DetectorEngine(this)
        detector = Detector(camera, detectorEngine)

        server = HttpServer(this, PORT, token(this), sensors, location, tts, stt, whisper, camera, dashcam, files, detector).also {
            try {
                it.start(NanoHTTPD.SOCKET_READ_TIMEOUT, false)
                running = true
            } catch (e: Exception) {
                Log.e(TAG, "server failed to start", e)
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int) = START_STICKY

    override fun onDestroy() {
        running = false
        server?.stop()
        detector.stop()
        detectorEngine.free()
        dashcam.shutdown()
        camera.shutdown()
        sensors.stop()
        location.stop()
        tts.shutdown()
        whisper.free()
        wakeLock?.takeIf { it.isHeld }?.release()
        super.onDestroy()
    }

    private fun goForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "DashLink server", NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val notification = Notification.Builder(this, CHANNEL)
            .setContentTitle("DashLink running")
            .setContentText("Serving sensors, GPS, speech, cameras and files on port $PORT")
            .setSmallIcon(android.R.drawable.stat_sys_upload_done)
            .setContentIntent(open)
            .setOngoing(true)
            .build()

        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification)
            return
        }
        var types = 0
        if (granted(Manifest.permission.ACCESS_FINE_LOCATION)) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && granted(Manifest.permission.RECORD_AUDIO)) {
            types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && granted(Manifest.permission.CAMERA)) {
            types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
        }
        try {
            startForeground(NOTIFICATION_ID, notification, types)
        } catch (e: Exception) {
            // Newer Androids refuse some types when started from boot; run without them.
            Log.w(TAG, "typed foreground start refused, retrying untyped", e)
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun granted(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    companion object {
        const val PORT = 8780
        private const val TAG = "DashLink"
        private const val CHANNEL = "dashlink"
        private const val NOTIFICATION_ID = 1

        @Volatile var running = false
            private set

        fun start(context: Context) {
            context.startForegroundService(Intent(context, DashLinkService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, DashLinkService::class.java))
        }

        /** Shared secret for non-loopback clients, generated once per install. */
        fun token(context: Context): String {
            val prefs = context.getSharedPreferences("dashlink", Context.MODE_PRIVATE)
            prefs.getString("token", null)?.let { return it }
            val bytes = ByteArray(12).also { SecureRandom().nextBytes(it) }
            val t = bytes.joinToString("") { "%02x".format(it) }
            prefs.edit().putString("token", t).apply()
            return t
        }
    }
}
