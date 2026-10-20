package dev.dashlink

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap

/** Keeps the latest reading of every motion/environment sensor we care about. */
class SensorHub(context: Context) : SensorEventListener {

    private val sm = context.getSystemService(SensorManager::class.java)

    // name -> (sensor type, sampling period)
    private val wanted = linkedMapOf(
        "accel" to (Sensor.TYPE_ACCELEROMETER to SensorManager.SENSOR_DELAY_GAME),
        "linear_accel" to (Sensor.TYPE_LINEAR_ACCELERATION to SensorManager.SENSOR_DELAY_GAME),
        "gravity" to (Sensor.TYPE_GRAVITY to SensorManager.SENSOR_DELAY_GAME),
        "gyro" to (Sensor.TYPE_GYROSCOPE to SensorManager.SENSOR_DELAY_GAME),
        "mag" to (Sensor.TYPE_MAGNETIC_FIELD to SensorManager.SENSOR_DELAY_GAME),
        "rotation" to (Sensor.TYPE_ROTATION_VECTOR to SensorManager.SENSOR_DELAY_GAME),
        "pressure" to (Sensor.TYPE_PRESSURE to SensorManager.SENSOR_DELAY_NORMAL),
        "light" to (Sensor.TYPE_LIGHT to SensorManager.SENSOR_DELAY_NORMAL),
    )
    private val nameByType = wanted.entries.associate { (name, v) -> v.first to name }

    /** Called with the peak in g when linear acceleration spikes (a crash or a very hard hit). */
    var onImpact: ((Float) -> Unit)? = null
    var impactThresholdG = 3.0f
    private var lastImpact = 0L

    private val values = ConcurrentHashMap<String, FloatArray>()
    private val timestamps = ConcurrentHashMap<String, Long>()

    val available: List<String>
        get() = wanted.filter { sm.getDefaultSensor(it.value.first) != null }.keys.toList()

    fun start() {
        for ((type, delay) in wanted.values) {
            sm.getDefaultSensor(type)?.let { sm.registerListener(this, it, delay) }
        }
    }

    fun stop() = sm.unregisterListener(this)

    override fun onSensorChanged(event: SensorEvent) {
        val name = nameByType[event.sensor.type] ?: return
        values[name] = event.values.clone()
        timestamps[name] = System.currentTimeMillis()
        if (name == "linear_accel") checkImpact(event.values)
    }

    private fun checkImpact(v: FloatArray) {
        val g = Math.sqrt((v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).toDouble()).toFloat() / SensorManager.GRAVITY_EARTH
        val now = System.currentTimeMillis()
        if (g >= impactThresholdG && now - lastImpact > 10_000) {
            lastImpact = now
            onImpact?.invoke(g)
        }
    }

    override fun onAccuracyChanged(sensor: Sensor, accuracy: Int) {}

    fun snapshot(): JSONObject {
        val out = JSONObject()
        val now = System.currentTimeMillis()
        for ((name, v) in values) {
            out.put(name, JSONObject()
                .put("values", v.toJson())
                .put("age_ms", now - (timestamps[name] ?: now)))
        }
        values["rotation"]?.let { rv ->
            // Orientation of the phone itself; the Pi applies the mounting offset.
            val r = FloatArray(9)
            val o = FloatArray(3)
            SensorManager.getRotationMatrixFromVector(r, rv)
            SensorManager.getOrientation(r, o)
            val azimuth = (Math.toDegrees(o[0].toDouble()) + 360.0) % 360.0
            out.put("orientation", JSONObject()
                .put("heading_deg", azimuth)
                .put("pitch_deg", Math.toDegrees(o[1].toDouble()))
                .put("roll_deg", Math.toDegrees(o[2].toDouble())))
        }
        values["pressure"]?.let { p ->
            out.put("baro_altitude_m",
                SensorManager.getAltitude(SensorManager.PRESSURE_STANDARD_ATMOSPHERE, p[0]).toDouble())
        }
        return out
    }
}

fun FloatArray.toJson(): JSONArray = JSONArray().also { a -> forEach { a.put(it.toDouble()) } }
