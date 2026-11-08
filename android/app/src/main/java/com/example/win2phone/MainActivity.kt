package com.example.win2phone

import android.app.Activity
import android.content.pm.ActivityInfo
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.media.MediaCodec
import android.media.MediaCodecList
import android.media.MediaFormat
import android.os.Bundle
import android.util.Log
import android.view.Gravity
import android.view.MotionEvent
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject
import java.io.BufferedReader
import java.io.BufferedWriter
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

/**
 * Win2Phone client (M0.2).
 *
 *  - TCP 19010 : [4B big-endian length][H.264 Annex-B NAL] -> MediaCodec -> SurfaceView
 *  - TCP 19011 : newline-delimited JSON control (touch, side, orientation, status)
 *
 * UI flow:
 *   page 1  choose transport: USB (connect immediately) or network (go to page 2)
 *   page 2  type the PC's LAN address and connect
 *   page 3  full-screen video with exactly ONE floating "disconnect" button
 *
 * M0.2 adds: two-stage connection UI, floating disconnect overlay, a persistent
 * control-channel reader (receives orientation/status pushed by the PC panel),
 * and automatic video reconnection when the host restarts its encoder.
 */
class MainActivity : Activity(), SurfaceHolder.Callback {

    companion object {
        private const val TAG = "Win2Phone"
        private const val VIDEO_PORT = 19010
        private const val CONTROL_PORT = 19011
        private const val CONNECT_TIMEOUT_MS = 3000
        private const val READ_TIMEOUT_MS = 15000
        private const val USB_HOST = "127.0.0.1"
        private const val PAGE_CONNECT = 0
        private const val PAGE_ADDRESS = 1
        private const val PAGE_STREAM = 2
        private const val PREFS = "win2phone"
        private const val KEY_LAST_HOST = "last_host"
    }

    private lateinit var pageConnect: View
    private lateinit var pageAddress: View
    private lateinit var pageStream: FrameLayout
    private lateinit var surface: SurfaceView
    private lateinit var connectStatus: TextView
    private lateinit var addressStatus: TextView
    private lateinit var hostEdit: EditText

    private var videoSocket: Socket? = null
    private var controlSocket: Socket? = null
    private var writer: BufferedWriter? = null

    @Volatile private var decoder: MediaCodec? = null
    private val running = AtomicBoolean(false)
    private val generation = AtomicInteger(0)
    @Volatile private var page = PAGE_CONNECT

    @Volatile private var surfaceReady = false
    @Volatile private var configured = false
    @Volatile private var frames = 0
    @Volatile private var dropped = 0
    @Volatile private var lastConfigAttempt = 0L
    @Volatile private var configFailures = 0
    @Volatile private var workingCodec = ""
    @Volatile private var workingFormat = ""
    @Volatile private var codecNamesLogged = false
    private var sps: ByteArray? = null
    private var pps: ByteArray? = null

    // Fallback only; the real dimensions come from the SPS (or the hello reply).
    private var width = 1280
    private var height = 720

    // ------------------------------------------------------------------ setup
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.decorView.systemUiVisibility = (
            View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            )
        setContentView(buildUi())
        surface.holder.addCallback(this)
        surface.setOnTouchListener { v, ev ->
            val nx = ev.x / v.width.toFloat()
            val ny = ev.y / v.height.toFloat()
            val action = when (ev.actionMasked) {
                MotionEvent.ACTION_DOWN -> "down"
                MotionEvent.ACTION_MOVE -> "move"
                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> "up"
                else -> null
            }
            if (action != null) {
                send("""{"type":"touch","action":"$action","x":$nx,"y":$ny}""")
            }
            true
        }
        showPage(PAGE_CONNECT)
        setStatus("未连接")
        Log.i(TAG, "onCreate")
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun buildUi(): View {
        val root = FrameLayout(this)

        pageConnect = buildConnectPage()
        pageAddress = buildAddressPage()
        pageStream = buildStreamPage()

        root.addView(pageConnect, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        root.addView(pageAddress, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        root.addView(pageStream, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))
        return root
    }

    private fun bigButton(label: String, onClick: () -> Unit): Button =
        Button(this).apply {
            text = label
            isAllCaps = false
            textSize = 17f
            setPadding(dp(20), dp(16), dp(20), dp(16))
            setOnClickListener { onClick() }
        }

    private fun title(text: String, size: Float): TextView =
        TextView(this).apply {
            this.text = text
            textSize = size
            setTextColor(Color.WHITE)
        }

    private fun hint(text: String): TextView =
        TextView(this).apply {
            this.text = text
            textSize = 12f
            setTextColor(Color.parseColor("#9E9E9E"))
        }

    /** Page 1: pick the transport. USB connects immediately. */
    private fun buildConnectPage(): View {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(28), dp(80), dp(28), dp(28))
        }
        box.addView(title("Win2Phone", 28f))
        box.addView(hint("把手机变成电脑的第二块屏"))
        box.addView(TextView(this).apply { height = dp(48) })

        box.addView(bigButton("USB 连接（推荐）") { startConnection(USB_HOST) })
        box.addView(TextView(this).apply { height = dp(12) })
        box.addView(bigButton("网络 / WiFi 连接") { showPage(PAGE_ADDRESS) })

        box.addView(TextView(this).apply { height = dp(28) })
        connectStatus = hint("未连接")
        box.addView(connectStatus)
        box.addView(TextView(this).apply { height = dp(16) })
        box.addView(hint(
            "USB 模式需要电脑先执行：\n" +
            "adb reverse tcp:19010 tcp:19010\n" +
            "adb reverse tcp:19011 tcp:19011\n" +
            "并保持 host.py 正在运行"))
        return box
    }

    /** Page 2: enter the PC LAN address. */
    private fun buildAddressPage(): View {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(28), dp(80), dp(28), dp(28))
        }
        box.addView(title("网络投屏", 24f))
        box.addView(hint("手机与电脑须在同一局域网"))
        box.addView(TextView(this).apply { height = dp(32) })

        hostEdit = EditText(this).apply {
            hint = "电脑局域网 IP，例如 192.168.1.10"
            setText(getSharedPreferences(PREFS, MODE_PRIVATE)
                .getString(KEY_LAST_HOST, "") ?: "")
            setTextColor(Color.WHITE)
            setHintTextColor(Color.parseColor("#757575"))
        }
        box.addView(hostEdit, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
        box.addView(TextView(this).apply { height = dp(20) })

        val row = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        row.addView(bigButton("开始投屏") {
            val h = hostEdit.text.toString().trim()
            if (h.isEmpty()) {
                toast("请填写电脑的 IP 地址")
            } else {
                getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                    .putString(KEY_LAST_HOST, h).apply()
                startConnection(h)
            }
        }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        row.addView(bigButton("返回") { showPage(PAGE_CONNECT) },
            LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
        box.addView(row, LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))

        box.addView(TextView(this).apply { height = dp(24) })
        addressStatus = hint("未连接")
        box.addView(addressStatus)
        return box
    }

    /** Page 3: full-screen video + exactly one floating disconnect button. */
    private fun buildStreamPage(): FrameLayout {
        val box = FrameLayout(this)
        surface = SurfaceView(this)
        // MediaOverlay keeps the surface above the window background but BELOW
        // normal views, so the floating button can be drawn on top of the video.
        // (setZOrderOnTop would cover the button.)
        surface.setZOrderMediaOverlay(true)
        box.addView(surface, FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT))

        val disconnect = Button(this).apply {
            text = "断开"
            isAllCaps = false
            textSize = 14f
            setTextColor(Color.WHITE)
            setPadding(dp(22), dp(10), dp(22), dp(10))
            background = GradientDrawable().apply {
                setColor(0xB3000000.toInt())
                cornerRadius = dp(22).toFloat()
            }
            alpha = 0.82f
            setOnClickListener { stopStreaming() }
        }
        val lp = FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT,
            Gravity.TOP or Gravity.END)
        lp.setMargins(dp(12), dp(20), dp(12), dp(12))
        box.addView(disconnect, lp)
        return box
    }

    // ------------------------------------------------------------------- pages
    private fun showPage(p: Int) {
        page = p
        runOnUiThread {
            pageConnect.visibility = if (p == PAGE_CONNECT) View.VISIBLE else View.GONE
            pageAddress.visibility = if (p == PAGE_ADDRESS) View.VISIBLE else View.GONE
            pageStream.visibility = if (p == PAGE_STREAM) View.VISIBLE else View.GONE
        }
    }

    private fun setStatus(text: String) {
        Log.i(TAG, "status: $text")
        runOnUiThread {
            connectStatus.text = text
            addressStatus.text = text
        }
    }

    private fun toast(text: String) = runOnUiThread {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show()
    }

    private fun isActive(gen: Int): Boolean = running.get() && generation.get() == gen

    @Synchronized
    private fun send(line: String) {
        try {
            writer?.write(line + "\n")
            writer?.flush()
        } catch (e: Exception) {
            Log.w(TAG, "control send failed: ${e.message}")
        }
    }

    private fun startConnection(host: String) {
        val h = host.trim()
        if (h.isEmpty()) { toast("请填写 PC 地址"); return }
        showPage(PAGE_STREAM)
        connect(h)
    }

    private fun stopStreaming() {
        disconnect()
        // Let the sensor decide again; the PC panel may have forced landscape/portrait.
        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
        showPage(PAGE_CONNECT)
        setStatus("已断开")
        toast("已断开")
    }

    @Suppress("MissingSuperCall")
    override fun onBackPressed() {
        when (page) {
            PAGE_STREAM -> stopStreaming()
            PAGE_ADDRESS -> showPage(PAGE_CONNECT)
            else -> super.onBackPressed()
        }
    }

    // ------------------------------------------------------------ connection
    private fun openControl(host: String, gen: Int): Socket {
        val cs = Socket()
        cs.connect(InetSocketAddress(host, CONTROL_PORT), CONNECT_TIMEOUT_MS)
        cs.soTimeout = 0                     // reader thread blocks until a line arrives
        controlSocket = cs
        writer = BufferedWriter(OutputStreamWriter(cs.getOutputStream(), Charsets.UTF_8))
        val reader = BufferedReader(InputStreamReader(cs.getInputStream(), Charsets.UTF_8))
        startControlReader(reader, gen, cs)
        send("""{"type":"hello","client":"android"}""")
        return cs
    }

    private fun openVideo(host: String): Socket {
        val vs = Socket()
        vs.connect(InetSocketAddress(host, VIDEO_PORT), CONNECT_TIMEOUT_MS)
        vs.tcpNoDelay = true
        vs.soTimeout = READ_TIMEOUT_MS
        return vs
    }

    private fun startControlReader(reader: BufferedReader, gen: Int, owner: Socket) {
        thread(name = "w2p-ctl-read") {
            try {
                while (isActive(gen)) {
                    val line = reader.readLine() ?: break
                    handleControl(line)
                }
            } catch (e: Exception) {
                Log.w(TAG, "control reader ended: ${e.javaClass.simpleName}: ${e.message}")
            } finally {
                try { owner.close() } catch (_: Exception) {}
                if (controlSocket === owner) {
                    controlSocket = null
                    writer = null
                }
            }
        }
    }

    private fun handleControl(line: String) {
        try {
            val o = JSONObject(line)
            when (val kind = o.optString("type")) {
                "hello", "status" -> {
                    val w = o.optInt("width", 0)
                    val h = o.optInt("height", 0)
                    if (w > 0 && h > 0 && !configured) {
                        width = w
                        height = h
                    }
                    Log.i(TAG, "control $kind ${w}x${h} "
                            + "side=${o.optString("side")} "
                            + "orientation=${o.optString("orientation")}")
                }
                "orientation" -> {
                    val portrait = o.optString("value") == "portrait"
                    runOnUiThread {
                        requestedOrientation = if (portrait)
                            ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
                        else
                            ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE
                    }
                    toast("PC 已切换为" + if (portrait) "竖屏" else "横屏")
                    Log.i(TAG, "PC requested ${if (portrait) "portrait" else "landscape"}")
                }
                "reconfigure" -> Log.i(TAG, "host will rebuild the video session")
                "error" -> toast("主机错误：" + o.optString("message"))
            }
        } catch (e: Exception) {
            Log.w(TAG, "bad control message: ${e.message}")
        }
    }

    private fun connect(host: String) {
        disconnect()
        val gen = generation.incrementAndGet()
        running.set(true)
        setStatus("连接中… $host")
        toast("连接中… $host")
        Log.i(TAG, "connect gen=$gen host=$host")

        thread(name = "w2p-session") {
            var ctl: Socket? = null
            while (isActive(gen)) {
                if (ctl == null || ctl.isClosed) {
                    ctl = try {
                        openControl(host, gen)
                    } catch (e: Exception) {
                        Log.w(TAG, "control connect failed: ${e.message}")
                        null
                    }
                }
                if (ctl == null) {
                    try { Thread.sleep(700) } catch (_: Exception) { break }
                    continue
                }

                var videoAttempts = 0
                while (isActive(gen) && !ctl!!.isClosed) {
                    try {
                        val vs = openVideo(host)
                        videoSocket = vs
                        Log.i(TAG, "video connected $host")
                        videoLoop(vs.getInputStream(), gen)
                    } catch (e: Exception) {
                        if (isActive(gen)) {
                            Log.w(TAG, "video error: ${e.javaClass.simpleName}: ${e.message}")
                        }
                    }
                    if (!isActive(gen)) break
                    resetVideo()
                    videoAttempts++
                    if (videoAttempts == 1) {
                        setStatus("视频流断开，正在重连…")
                    }
                    try { Thread.sleep(700) } catch (_: Exception) { break }
                }
                if (!isActive(gen)) break
                Log.i(TAG, "control channel lost, reopening")
                ctl = null
                try { Thread.sleep(700) } catch (_: Exception) { break }
            }
            Log.i(TAG, "session ended gen=$gen")
        }

        thread(name = "w2p-watchdog") {
            try { Thread.sleep(4000) } catch (_: Exception) { return@thread }
            if (isActive(gen) && frames == 0) {
                toast("4 秒仍无画面，请确认 PC 端 host.py 正在运行")
                setStatus("无画面：请检查主机服务与 USB 映射")
            }
        }
    }

    private fun resetVideo() {
        try { decoder?.stop(); decoder?.release() } catch (_: Exception) {}
        decoder = null
        configured = false
        sps = null
        pps = null
        frames = 0
        dropped = 0
        configFailures = 0
        lastConfigAttempt = 0L
        try { videoSocket?.close() } catch (_: Exception) {}
        videoSocket = null
    }

    private fun disconnect() {
        running.set(false)
        generation.incrementAndGet()
        resetVideo()
        try { controlSocket?.close() } catch (_: Exception) {}
        controlSocket = null
        writer = null
        Log.i(TAG, "disconnected")
    }

    override fun onDestroy() {
        disconnect()
        super.onDestroy()
    }

    // --------------------------------------------------------------- surface
    override fun surfaceCreated(holder: SurfaceHolder) {
        surfaceReady = true
        Log.i(TAG, "surfaceCreated")
    }

    override fun surfaceChanged(holder: SurfaceHolder, f: Int, w: Int, h: Int) {
        surfaceReady = true
        Log.i(TAG, "surfaceChanged ${w}x${h}")
    }

    override fun surfaceDestroyed(holder: SurfaceHolder) {
        surfaceReady = false
        // The Surface is gone: any decoder bound to it renders nowhere.
        try { decoder?.stop(); decoder?.release() } catch (_: Exception) {}
        decoder = null
        configured = false
        Log.i(TAG, "surfaceDestroyed -> decoder will be rebuilt")
    }

    // ------------------------------------------------------------ Annex-B I/O
    private fun readFully(input: InputStream, b: ByteArray): Boolean {
        var off = 0
        while (off < b.size) {
            val n = input.read(b, off, b.size - off)
            if (n < 0) return false
            off += n
        }
        return true
    }

    /** Strip the 3/4-byte start code; keep the NAL header byte. */
    private fun nalUnit(nal: ByteArray): ByteArray {
        var i = 0
        while (i < nal.size && nal[i] == 0.toByte()) i++
        if (i < nal.size && nal[i] == 1.toByte()) i++
        return if (i < nal.size) nal.copyOfRange(i, nal.size) else nal
    }

    /** RBSP bytes of a NAL (skip header, drop emulation-prevention bytes). */
    private fun rbsp(nal: ByteArray): ByteArray {
        val u = nalUnit(nal)
        if (u.isEmpty()) return ByteArray(0)
        val out = ByteArrayOutputStream()
        var zeros = 0
        for (k in 1 until u.size) {
            val b = u[k].toInt() and 0xFF
            if (zeros >= 2 && b == 3) { zeros = 0; continue }
            out.write(b)
            zeros = if (b == 0) zeros + 1 else 0
        }
        return out.toByteArray()
    }

    private fun startCode(): ByteArray = byteArrayOf(0, 0, 0, 1)

    private class BitReader(private val d: ByteArray) {
        private var pos = 0
        fun u(n: Int): Int {
            var v = 0
            repeat(n) {
                val byte = d[pos ushr 3].toInt() and 0xFF
                v = (v shl 1) or ((byte shr (7 - (pos and 7))) and 1)
                pos++
            }
            return v
        }
        fun ue(): Int {
            var z = 0
            while (u(1) == 0) {
                z++
                if (z > 31) throw IllegalStateException("bad exp-golomb")
            }
            return if (z == 0) 0 else (1 shl z) - 1 + u(z)
        }
        fun se(): Int {
            val k = ue()
            return if (k % 2 == 0) -(k / 2) else (k + 1) / 2
        }
    }

    /** Parse width/height out of an H.264 SPS. Returns null on malformed data. */
    private fun parseSpsSize(nal: ByteArray): Pair<Int, Int>? = try {
        val r = BitReader(rbsp(nal))
        val profile = r.u(8)
        r.u(8)                       // constraint flags
        r.u(8)                       // level_idc
        r.ue()                       // seq_parameter_set_id
        var chroma = 1
        if (profile in listOf(100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135)) {
            chroma = r.ue()
            if (chroma == 3) r.u(1)  // separate_colour_plane_flag
            r.ue()                   // bit_depth_luma_minus8
            r.ue()                   // bit_depth_chroma_minus8
            r.u(1)                   // qpprime_y_zero_transform_bypass_flag
            if (r.u(1) == 1) {       // seq_scaling_matrix_present_flag
                repeat(if (chroma != 3) 8 else 12) { i ->
                    if (r.u(1) == 1) {
                        val size = if (i < 6) 16 else 64
                        var last = 8
                        var next = 8
                        repeat(size) {
                            if (next != 0) next = (last + r.se() + 256) % 256
                            if (next != 0) last = next
                        }
                    }
                }
            }
        }
        r.ue()                       // log2_max_frame_num_minus4
        when (r.ue()) {              // pic_order_cnt_type
            0 -> r.ue()
            1 -> {
                r.u(1); r.se(); r.se()
                repeat(r.ue()) { r.se() }
            }
        }
        r.ue()                       // max_num_ref_frames
        r.u(1)                       // gaps_in_frame_num_value_allowed_flag
        val widthMbs = r.ue() + 1
        val heightMapUnits = r.ue() + 1
        val frameMbsOnly = r.u(1)
        if (frameMbsOnly == 0) r.u(1)  // mb_adaptive_frame_field_flag
        r.u(1)                       // direct_8x8_inference_flag
        var cl = 0; var cr = 0; var ct = 0; var cb = 0
        if (r.u(1) == 1) {           // frame_cropping_flag
            cl = r.ue(); cr = r.ue(); ct = r.ue(); cb = r.ue()
        }
        val subW = if (chroma == 1 || chroma == 2) 2 else 1
        val subH = if (chroma == 1) 2 else 1
        val w = widthMbs * 16 - (cl + cr) * subW
        val h = (2 - frameMbsOnly) * heightMapUnits * 16 - (ct + cb) * subH
        if (w > 0 && h > 0) w to h else null
    } catch (e: Exception) {
        Log.w(TAG, "SPS parse failed: ${e.message}")
        null
    }

    private fun videoLoop(input: InputStream, gen: Int) {
        val header = ByteArray(4)
        try {
            while (isActive(gen)) {
                if (!readFully(input, header)) break
                val len = ByteBuffer.wrap(header).int
                if (len <= 0 || len > 4 * 1024 * 1024) {
                    Log.e(TAG, "invalid NAL length $len")
                    break
                }
                val nal = ByteArray(len)
                if (!readFully(input, nal)) break
                handleNal(nal, gen)
            }
        } catch (e: Exception) {
            Log.w(TAG, "video loop ended: ${e.javaClass.simpleName}: ${e.message}")
        }
        Log.i(TAG, "video stream ended gen=$gen frames=$frames")
    }

    private fun handleNal(nal: ByteArray, gen: Int) {
        if (nal.size < 4) return
        val type = nal[3].toInt() and 0x1F
        when (type) {
            7 -> {
                sps = nal
                parseSpsSize(nal)?.let {
                    width = it.first
                    height = it.second
                }
                Log.i(TAG, "SPS -> ${width}x${height} (${nal.size}B)")
            }
            8 -> {
                pps = nal
                Log.i(TAG, "PPS (${nal.size}B)")
            }
            else -> {
                if (!configured) {
                    val now = System.currentTimeMillis()
                    if (now - lastConfigAttempt < 200) return
                    lastConfigAttempt = now
                    if (!configureDecoder(gen)) return
                }
                feed(nal, type == 5)
            }
        }
    }

    private fun avcDecoderNames(): List<String> {
        val names = mutableListOf<String>()
        try {
            for (info in MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos) {
                if (info.isEncoder) continue
                if (info.supportedTypes.any {
                        it.equals(MediaFormat.MIMETYPE_VIDEO_AVC, ignoreCase = true)
                    }) {
                    names.add(info.name)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "codec enumeration failed: ${e.message}")
        }
        if (names.isEmpty()) names.add("")  // "" -> createDecoderByType
        if (workingCodec.isNotEmpty()) {
            names.remove(workingCodec)
            names.add(0, workingCodec)
        }
        if (!codecNamesLogged) {
            codecNamesLogged = true
            Log.i(TAG, "AVC decoders: $names")
        }
        return names
    }

    private fun configureDecoder(gen: Int): Boolean {
        val s = sps ?: return false
        val p = pps ?: return false
        if (!surfaceReady) return false
        val sf = try { surface.holder.surface } catch (e: Exception) { null }
        if (sf == null || !sf.isValid) return false

        val sNal = nalUnit(s)
        val pNal = nalUnit(p)
        val sc = startCode()
        // Vendor C2/OMX decoders disagree on csd packing and some reject
        // `createDecoderByType`'s first choice (BAD_VALUE). Enumerate them all;
        // in-band SPS/PPS on every IDR keeps the "bare" format complete.
        val formats = mutableListOf<Pair<String, (MediaFormat) -> Unit>>(
            "bare" to { },
            "csd-raw" to { f ->
                f.setByteBuffer("csd-0", ByteBuffer.wrap(sNal))
                f.setByteBuffer("csd-1", ByteBuffer.wrap(pNal))
                f.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 2 * 1024 * 1024)
            },
            "csd-annexb" to { f ->
                f.setByteBuffer("csd-0", ByteBuffer.wrap(sc + sNal))
                f.setByteBuffer("csd-1", ByteBuffer.wrap(sc + pNal))
                f.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 2 * 1024 * 1024)
            },
        )
        if (workingFormat.isNotEmpty()) {
            val idx = formats.indexOfFirst { it.first == workingFormat }
            if (idx > 0) formats.add(0, formats.removeAt(idx))
        }

        for (codec in avcDecoderNames()) {
            for ((fname, apply) in formats) {
                var pending: MediaCodec? = null
                try {
                    val fmt = MediaFormat.createVideoFormat(
                        MediaFormat.MIMETYPE_VIDEO_AVC, width, height)
                    apply(fmt)
                    pending = if (codec.isEmpty()) {
                        MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
                    } else {
                        MediaCodec.createByCodecName(codec)
                    }
                    val d = pending
                    d.configure(fmt, sf, null, 0)
                    d.start()
                    workingCodec = codec
                    workingFormat = fname
                    decoder = d
                    configured = true
                    Log.i(TAG, "decoder configured ${width}x${height} "
                            + "codec=${codec.ifEmpty { "default" }} fmt=$fname "
                            + "retries=$configFailures")
                    thread(name = "w2p-output") { drainOutput(d, gen) }
                    return true
                } catch (e: Exception) {
                    try { pending?.release() } catch (_: Exception) {}
                    Log.w(TAG, "configure ${codec.ifEmpty { "default" }}/$fname failed: "
                            + "${e.javaClass.simpleName}: ${e.message}")
                }
            }
        }
        configFailures++
        return false
    }

    private fun feed(nal: ByteArray, keyFrame: Boolean) {
        val d = decoder ?: return
        val sc = startCode()
        val s = sps
        val p = pps
        val data = if (keyFrame && s != null && p != null) {
            sc + nalUnit(s) + sc + nalUnit(p) + sc + nalUnit(nal)
        } else {
            sc + nalUnit(nal)
        }
        try {
            val idx = d.dequeueInputBuffer(if (keyFrame) 50000 else 10000)
            if (idx < 0) {
                dropped++
                if (dropped % 50 == 1) {
                    Log.w(TAG, "input buffer busy, dropped $dropped NALs so far")
                }
                return
            }
            val buf = d.getInputBuffer(idx) ?: return
            buf.clear()
            if (buf.capacity() < data.size) return
            buf.put(data)
            d.queueInputBuffer(idx, 0, data.size, System.nanoTime() / 1000L, 0)
        } catch (e: Exception) {
            Log.w(TAG, "feed failed: ${e.message}")
        }
    }

    private fun drainOutput(d: MediaCodec, gen: Int) {
        val info = MediaCodec.BufferInfo()
        while (isActive(gen) && decoder === d) {
            try {
                when (val idx = d.dequeueOutputBuffer(info, 10000)) {
                    MediaCodec.INFO_TRY_AGAIN_LATER,
                    MediaCodec.INFO_OUTPUT_BUFFERS_CHANGED -> Unit
                    MediaCodec.INFO_OUTPUT_FORMAT_CHANGED ->
                        Log.i(TAG, "output format: ${d.outputFormat}")
                    else -> if (idx >= 0) {
                        frames++
                        if (frames == 1) {
                            Log.i(TAG, "first frame rendered ${width}x${height}")
                            toast("已出画 ${width}x${height}")
                        } else if (frames % 120 == 0) {
                            Log.d(TAG, "frames=$frames")
                        }
                        d.releaseOutputBuffer(idx, true)
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "output loop ended: ${e.message}")
                break
            }
        }
        Log.i(TAG, "output loop exit gen=$gen frames=$frames")
    }
}
