package dev.dashlink

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.GnssStatus
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import org.json.JSONObject

/** GPS (plus network fallback) fixes and satellite counts. */
class LocationHub(private val context: Context) : LocationListener {

    private val lm = context.getSystemService(LocationManager::class.java)
    private val main = Handler(Looper.getMainLooper())

    @Volatile private var last: Location? = null
    @Volatile private var satsUsed = 0
    @Volatile private var satsVisible = 0

    private val gnssCallback = object : GnssStatus.Callback() {
        override fun onSatelliteStatusChanged(status: GnssStatus) {
            satsVisible = status.satelliteCount
            satsUsed = (0 until status.satelliteCount).count { status.usedInFix(it) }
        }
    }

    val permitted: Boolean
        get() = context.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    @SuppressLint("MissingPermission")
    fun start() {
        if (!permitted) return
        for (provider in listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)) {
            if (lm.allProviders.contains(provider)) {
                lm.requestLocationUpdates(provider, 1000L, 0f, this, Looper.getMainLooper())
            }
        }
        @Suppress("DEPRECATION")
        lm.registerGnssStatusCallback(gnssCallback, main)
        last = lm.getLastKnownLocation(LocationManager.GPS_PROVIDER)
    }

    fun stop() {
        lm.removeUpdates(this)
        lm.unregisterGnssStatusCallback(gnssCallback)
    }

    override fun onLocationChanged(location: Location) {
        val prev = last
        // Prefer GPS; only take a network fix when GPS has gone stale.
        if (prev == null ||
            location.provider == LocationManager.GPS_PROVIDER ||
            prev.provider != LocationManager.GPS_PROVIDER ||
            location.time - prev.time > 5000
        ) {
            last = location
        }
    }

    // Android 10 has no default implementations for these; leaving them out crashes there.
    @Deprecated("Deprecated in Java")
    override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
    override fun onProviderEnabled(provider: String) {}
    override fun onProviderDisabled(provider: String) {}

    fun snapshot(): JSONObject {
        val out = JSONObject()
            .put("permitted", permitted)
            .put("sats_used", satsUsed)
            .put("sats_visible", satsVisible)
        val l = last ?: return out.put("fix", false)
        return out
            .put("fix", true)
            .put("provider", l.provider)
            .put("lat", l.latitude)
            .put("lon", l.longitude)
            .put("accuracy_m", l.accuracy.toDouble())
            .put("altitude_m", if (l.hasAltitude()) l.altitude else JSONObject.NULL)
            .put("speed_mps", if (l.hasSpeed()) l.speed.toDouble() else JSONObject.NULL)
            .put("bearing_deg", if (l.hasBearing()) l.bearing.toDouble() else JSONObject.NULL)
            .put("time", l.time)
            .put("age_ms", System.currentTimeMillis() - l.time)
    }
}
