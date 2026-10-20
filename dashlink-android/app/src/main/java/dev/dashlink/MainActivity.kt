package dev.dashlink

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/** Setup screen: grants permissions, starts the service, shows how the Pi should connect. */
class MainActivity : Activity() {

    private lateinit var status: TextView
    private val ui = Handler(Looper.getMainLooper())
    private val refresher = object : Runnable {
        override fun run() {
            refresh()
            ui.postDelayed(this, 2000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val pad = (16 * resources.displayMetrics.density).toInt()
        status = TextView(this).apply {
            typeface = Typeface.MONOSPACE
            textSize = 14f
        }
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
            addView(button("Start server") { requestThenStart() })
            addView(button("Stop server") { DashLinkService.stop(this@MainActivity) })
            addView(button("Allow running in background") { askBatteryExemption() })
            addView(status)
        }
        setContentView(ScrollView(this).apply { addView(root) })
        requestThenStart()
    }

    override fun onResume() {
        super.onResume()
        ui.post(refresher)
    }

    override fun onPause() {
        ui.removeCallbacks(refresher)
        super.onPause()
    }

    private fun button(label: String, onClick: () -> Unit) =
        Button(this).apply {
            text = label
            setOnClickListener { onClick() }
        }

    private fun requestThenStart() {
        val needed = buildList {
            add(Manifest.permission.RECORD_AUDIO)
            add(Manifest.permission.ACCESS_FINE_LOCATION)
            add(Manifest.permission.CAMERA)
            if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
        }.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }

        if (needed.isEmpty()) {
            DashLinkService.start(this)
        } else {
            requestPermissions(needed.toTypedArray(), 1)
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        // Start regardless; whatever was denied simply reports as unavailable.
        DashLinkService.start(this)
    }

    private fun askBatteryExemption() {
        val pm = getSystemService(PowerManager::class.java)
        if (!pm.isIgnoringBatteryOptimizations(packageName)) {
            startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:$packageName")))
        }
    }

    private fun refresh() {
        val s = DeviceStatus.snapshot(this)
        val addrs = s.getJSONObject("addresses")
        val urls = addrs.keys().asSequence().joinToString("\n") { "  http://${addrs.getString(it)}:${DashLinkService.PORT}  ($it)" }
        val battery = s.getJSONObject("battery")
        val pm = getSystemService(PowerManager::class.java)
        status.text = buildString {
            appendLine()
            appendLine("Server:   ${if (DashLinkService.running) "RUNNING" else "stopped"} on port ${DashLinkService.PORT}")
            appendLine("Token:    ${DashLinkService.token(this@MainActivity)}")
            appendLine("Battery:  ${battery.opt("percent")}%  ${battery.opt("temp_c")}°C  ${battery.opt("plugged")}")
            appendLine("Hottest:  ${s.opt("max_thermal_c")}°C")
            appendLine("Bg OK:    ${pm.isIgnoringBatteryOptimizations(packageName)}")
            appendLine()
            appendLine("Network URLs (need ?token= or Bearer header):")
            appendLine(urls.ifEmpty { "  (no network)" })
            appendLine()
            appendLine("From the Pi over USB (no token needed):")
            appendLine("  adb forward tcp:${DashLinkService.PORT} tcp:${DashLinkService.PORT}")
            appendLine("  curl localhost:${DashLinkService.PORT}/all")
        }
    }
}
