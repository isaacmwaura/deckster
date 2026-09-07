package com.streamcontrol.shell

import android.Manifest
import android.animation.ValueAnimator
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PorterDuff
import android.graphics.PorterDuffXfermode
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.view.animation.LinearInterpolator
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.addCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * Native QR scan for Wi-Fi pairing. The PC's /qr encodes the connect URL with the
 * pairing code (`...?pair=CODE`), so we hand the decoded URL straight back and the
 * WebView pairs on load. Doing this natively (not with the in-page camera) is what
 * lets it work over plain-HTTP Wi-Fi, where the browser blocks camera access.
 *
 * The screen shows a targeting frame (with a sweeping scan line) the user aims the QR
 * into: only a code whose centre falls inside that frame is accepted. There's an
 * always-visible Close chip, Back also cancels, and the screen is kept awake.
 */
class QrScannerActivity : AppCompatActivity() {

    private lateinit var previewView: PreviewView
    private lateinit var overlay: ReticleOverlay
    private lateinit var cameraExecutor: ExecutorService
    private val scanner = BarcodeScanning.getClient(
        BarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
    )
    @Volatile private var handled = false

    private val requestCamera = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) startCamera()
        else { Toast.makeText(this, "Camera needed to scan", Toast.LENGTH_LONG).show(); cancel() }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setResult(RESULT_CANCELED)                       // default outcome unless we deliver

        previewView = PreviewView(this)
        overlay = ReticleOverlay(this)
        // Build explicitly (not via apply): inside an apply on the FrameLayout, `overlay`
        // would resolve to View.getOverlay() rather than our field.
        val root = FrameLayout(this)
        root.addView(previewView, matchParent())
        root.addView(overlay, matchParent())
        root.addView(closeButton(), topStart())
        setContentView(root)

        // Belt-and-braces: an explicit back callback so leaving is always possible,
        // even where a gesture is swallowed by the preview surface.
        onBackPressedDispatcher.addCallback(this) { cancel() }

        cameraExecutor = Executors.newSingleThreadExecutor()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED) {
            startCamera()
        } else {
            requestCamera.launch(Manifest.permission.CAMERA)
        }
    }

    private fun startCamera() {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            val provider = future.get()
            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(previewView.surfaceProvider)
            }
            val analysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
                .also { it.setAnalyzer(cameraExecutor, ::analyze) }
            try {
                provider.unbindAll()
                provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis)
            } catch (e: Exception) {
                finish()
            }
        }, ContextCompat.getMainExecutor(this))
    }

    @OptIn(ExperimentalGetImage::class)
    private fun analyze(proxy: ImageProxy) {
        val media = proxy.image
        if (media == null || handled) { proxy.close(); return }
        val image = InputImage.fromMediaImage(media, proxy.imageInfo.rotationDegrees)
        val w = image.width.toFloat()
        val h = image.height.toFloat()
        scanner.process(image)
            .addOnSuccessListener { codes ->
                // Accept only a QR aimed into the central frame (centre in the middle
                // ~60% of the image on both axes), so stray codes aren't grabbed.
                val hit = codes.firstOrNull { c ->
                    val b = c.boundingBox ?: return@firstOrNull false
                    val cx = b.exactCenterX() / w
                    val cy = b.exactCenterY() / h
                    cx in 0.20f..0.80f && cy in 0.20f..0.80f
                }
                val raw = hit?.rawValue
                if (!raw.isNullOrBlank() && !handled) { handled = true; overlay.flashLock(); deliver(raw) }
            }
            .addOnCompleteListener { proxy.close() }
    }

    private fun deliver(raw: String) {
        setResult(RESULT_OK, Intent().putExtra(EXTRA_URL, raw.trim()))
        finish()
    }

    private fun cancel() {
        setResult(RESULT_CANCELED)
        finish()
    }

    override fun onDestroy() {
        super.onDestroy()
        if (::cameraExecutor.isInitialized) cameraExecutor.shutdown()
    }

    // ---- view construction (no XML) -----------------------------------------
    private fun matchParent() = FrameLayout.LayoutParams(
        FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT
    )

    private fun closeButton() = TextView(this).apply {
        text = "✕"
        setTextColor(Color.WHITE)
        textSize = 18f
        gravity = Gravity.CENTER
        contentDescription = "Close"
        background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(0x73000000)                          // translucent black chip
            setStroke(dp(1), 0x33FFFFFF)
        }
        setOnClickListener { cancel() }
    }

    private fun topStart() = FrameLayout.LayoutParams(dp(44), dp(44)).apply {
        gravity = Gravity.TOP or Gravity.START; topMargin = dp(16); leftMargin = dp(16)
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    companion object { const val EXTRA_URL = "url" }
}

/**
 * Dark scrim with a clear, bracket-cornered square to aim the QR into, a subtle full
 * border, a sweeping accent scan line, and framed title/hint text. The scan line
 * animates while the camera is live and stops (frame turns green) on a lock.
 */
private class ReticleOverlay(context: Context) : View(context) {
    private val accentColor = 0xFF56C2FF.toInt()
    private val okColor = 0xFF4DDB7F.toInt()

    private val scrim = Paint().apply { color = 0xC0000000.toInt() }              // ~75% black
    private val clear = Paint().apply { xfermode = PorterDuffXfermode(PorterDuff.Mode.CLEAR) }
    private val border = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; color = 0x40FFFFFF; strokeWidth = dp(1.5f)
    }
    private val corner = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; color = accentColor; strokeWidth = dp(4f)
        strokeCap = Paint.Cap.ROUND
    }
    private val scanLine = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = accentColor; strokeWidth = dp(2.5f); strokeCap = Paint.Cap.ROUND
    }
    private val glow = Paint(Paint.ANTI_ALIAS_FLAG)
    private val titlePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE; textAlign = Paint.Align.CENTER
        textSize = sp(17f); typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
        setShadowLayer(dp(5f), 0f, dp(1f), 0xB3000000.toInt())
    }
    private val hintPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = 0xFFC2C6CF.toInt(); textAlign = Paint.Align.CENTER
        textSize = sp(12.5f); setShadowLayer(dp(5f), 0f, dp(1f), 0xB3000000.toInt())
    }

    private var scan = 0f
    private var locked = false
    private val anim = ValueAnimator.ofFloat(0f, 1f).apply {
        duration = 2000L
        repeatCount = ValueAnimator.INFINITE
        repeatMode = ValueAnimator.REVERSE
        interpolator = LinearInterpolator()
        addUpdateListener { scan = it.animatedValue as Float; invalidate() }
    }

    init { setLayerType(LAYER_TYPE_HARDWARE, null) }

    override fun onAttachedToWindow() { super.onAttachedToWindow(); anim.start() }
    override fun onDetachedFromWindow() { anim.cancel(); super.onDetachedFromWindow() }

    /** Freeze the sweep and turn the frame green — a brief confirmation on a successful read. */
    fun flashLock() { locked = true; anim.cancel(); invalidate() }

    override fun onDraw(canvas: Canvas) {
        val side = 0.64f * minOf(width, height)
        val left = (width - side) / 2f
        val top = (height - side) / 2f
        val r = RectF(left, top, left + side, top + side)
        val radius = dp(24f)
        val active = if (locked) okColor else accentColor

        // dim everything, then punch a clear rounded window for the camera
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), scrim)
        canvas.drawRoundRect(r, radius, radius, clear)

        // subtle full border for definition
        canvas.drawRoundRect(r, radius, radius, border)

        // sweeping scan line (clipped to the frame), skipped once locked
        if (!locked) {
            val m = dp(8f)
            val yTop = r.top + m
            val yBot = r.bottom - m
            val y = yTop + scan * (yBot - yTop)
            val gh = dp(56f)
            canvas.save()
            val clip = Path().apply { addRoundRect(r, radius, radius, Path.Direction.CW) }
            canvas.clipPath(clip)
            glow.shader = LinearGradient(0f, y - gh, 0f, y,
                accentColor and 0x00FFFFFF, (accentColor and 0xFFFFFF) or 0x5A000000, Shader.TileMode.CLAMP)
            canvas.drawRect(r.left, y - gh, r.right, y, glow)
            canvas.drawLine(r.left + m, y, r.right - m, y, scanLine)
            canvas.restore()
        }

        // accent corner brackets
        corner.color = active
        val c = dp(28f)
        // top-left
        canvas.drawLine(r.left, r.top + c, r.left, r.top + radius, corner)
        canvas.drawLine(r.left + radius, r.top, r.left + c, r.top, corner)
        // top-right
        canvas.drawLine(r.right, r.top + c, r.right, r.top + radius, corner)
        canvas.drawLine(r.right - radius, r.top, r.right - c, r.top, corner)
        // bottom-left
        canvas.drawLine(r.left, r.bottom - c, r.left, r.bottom - radius, corner)
        canvas.drawLine(r.left + radius, r.bottom, r.left + c, r.bottom, corner)
        // bottom-right
        canvas.drawLine(r.right, r.bottom - c, r.right, r.bottom - radius, corner)
        canvas.drawLine(r.right - radius, r.bottom, r.right - c, r.bottom, corner)

        // title above the frame, hint below — composed with the reticle
        canvas.drawText("Point at the QR on your PC", width / 2f, r.top - dp(24f), titlePaint)
        val hint = if (locked) "Pairing…" else "It pairs automatically once it's in the frame"
        canvas.drawText(hint, width / 2f, r.bottom + dp(34f), hintPaint)
    }

    private fun dp(v: Float) = v * resources.displayMetrics.density
    private fun sp(v: Float) =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_SP, v, resources.displayMetrics)
}
