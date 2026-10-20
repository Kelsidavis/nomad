package dev.dashlink

import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.SystemClock
import org.json.JSONObject
import java.io.File
import java.net.Inet4Address
import java.net.NetworkInterface

/** Battery, thermal and memory state so the Pi can cut power or back off when the dash gets hot. */
object DeviceStatus {

    fun snapshot(context: Context): JSONObject {
        val battery = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        val level = battery?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1) ?: -1
        val scale = battery?.getIntExtra(BatteryManager.EXTRA_SCALE, 100) ?: 100
        val plugged = battery?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0

        val mem = ActivityManager.MemoryInfo()
        context.getSystemService(ActivityManager::class.java).getMemoryInfo(mem)

        return JSONObject()
            .put("battery", JSONObject()
                .put("percent", if (level >= 0) level * 100 / scale else JSONObject.NULL)
                .put("temp_c", (battery?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, 0) ?: 0) / 10.0)
                .put("voltage_mv", battery?.getIntExtra(BatteryManager.EXTRA_VOLTAGE, 0) ?: 0)
                .put("health", healthName(battery?.getIntExtra(BatteryManager.EXTRA_HEALTH, 0) ?: 0))
                .put("plugged", when (plugged) {
                    BatteryManager.BATTERY_PLUGGED_AC -> "ac"
                    BatteryManager.BATTERY_PLUGGED_USB -> "usb"
                    BatteryManager.BATTERY_PLUGGED_WIRELESS -> "wireless"
                    else -> "none"
                }))
            .put("max_thermal_c", maxThermal() ?: JSONObject.NULL)
            .put("mem_available_mb", mem.availMem / 1_048_576)
            .put("uptime_s", SystemClock.elapsedRealtime() / 1000)
            .put("addresses", addresses())
    }

    private fun healthName(h: Int) = when (h) {
        BatteryManager.BATTERY_HEALTH_GOOD -> "good"
        BatteryManager.BATTERY_HEALTH_OVERHEAT -> "overheat"
        BatteryManager.BATTERY_HEALTH_DEAD -> "dead"
        BatteryManager.BATTERY_HEALTH_OVER_VOLTAGE -> "over_voltage"
        BatteryManager.BATTERY_HEALTH_COLD -> "cold"
        else -> "unknown"
    }

    // Zone types that are real temperature sensors; others (e.g. "lmh-dcvs") report fixed limits.
    private val realZone = Regex("cpu|gpu|kryo|therm|skin|battery|pa", RegexOption.IGNORE_CASE)

    /** Hottest real thermal sensor. Some zones are unreadable or report junk, so filter. */
    fun maxThermal(): Double? =
        File("/sys/class/thermal").listFiles { f -> f.name.startsWith("thermal_zone") }
            ?.filter { z -> runCatching { File(z, "type").readText() }.getOrDefault("").let { realZone.containsMatchIn(it) && !it.contains("lmh") } }
            ?.mapNotNull { runCatching { File(it, "temp").readText().trim().toDouble() }.getOrNull() }
            ?.map { if (it > 1000) it / 1000.0 else it }
            ?.filter { it in 1.0..150.0 }
            ?.maxOrNull()

    fun addresses(): JSONObject {
        val out = JSONObject()
        runCatching {
            for (iface in NetworkInterface.getNetworkInterfaces()) {
                if (!iface.isUp || iface.isLoopback) continue
                iface.inetAddresses.toList().filterIsInstance<Inet4Address>().firstOrNull()?.let {
                    out.put(iface.name, it.hostAddress)
                }
            }
        }
        return out
    }
}
