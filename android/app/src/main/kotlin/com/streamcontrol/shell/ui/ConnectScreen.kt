package com.streamcontrol.shell.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.streamcontrol.shell.Pc

private val BG = Color(0xFF0B0E14)
private val CARD = Color(0xFF14161B)
private val INK = Color(0xFFF2F3F5)
private val SUB = Color(0xFF9A9EA8)
private val BLUE = Color(0xFF56C2FF)

/**
 * Shown when USB isn't present: lists PCs found on Wi-Fi (mDNS), plus Scan QR and a
 * manual address field. USB is still the primary path — this is the wireless fallback.
 */
@Composable
fun ConnectScreen(
    devices: List<Pc>,
    onPick: (Pc) -> Unit,
    onScan: () -> Unit,
    onManual: (String) -> Unit,
    onRetry: () -> Unit,
) {
    var manual by remember { mutableStateOf("") }
    Column(
        modifier = Modifier
            .fillMaxSize()
            .background(BG)
            .verticalScroll(rememberScrollState())
            .padding(28.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("Deckster", color = INK, fontSize = 22.sp, fontWeight = FontWeight.Bold)
        Text("Connect to your PC", color = SUB, fontSize = 14.sp)
        Spacer(Modifier.height(22.dp))

        if (devices.isNotEmpty()) {
            SectionLabel("FOUND ON WI-FI")
            devices.forEach { pc ->
                Card(
                    colors = CardDefaults.cardColors(containerColor = CARD),
                    shape = RoundedCornerShape(14.dp),
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 5.dp)
                        .clickable { onPick(pc) },
                ) {
                    Column(Modifier.padding(14.dp)) {
                        Text(pc.name, color = INK, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                        Text(pc.url, color = SUB, fontSize = 12.sp)
                    }
                }
            }
            Spacer(Modifier.height(16.dp))
        } else {
            Text(
                "Looking for your PC over USB and Wi-Fi…",
                color = SUB, fontSize = 13.sp, textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(16.dp))
        }

        Button(
            onClick = onScan,
            colors = ButtonDefaults.buttonColors(containerColor = BLUE, contentColor = Color(0xFF08121A)),
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Scan QR code", fontWeight = FontWeight.Bold) }

        Spacer(Modifier.height(18.dp))
        SectionLabel("OR ENTER THE PC ADDRESS")
        OutlinedTextField(
            value = manual,
            onValueChange = { manual = it },
            singleLine = true,
            placeholder = { Text("http://192.168.1.20:8765/", color = SUB) },
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        Button(
            onClick = { if (manual.isNotBlank()) onManual(manual) },
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Connect") }

        Spacer(Modifier.height(6.dp))
        TextButton(onClick = onRetry) { Text("Retry USB", color = SUB) }
    }
}

/**
 * Shown instead of the browser's "webpage not available" when the agent can't be
 * reached, with Refresh (reload) and Back (return to the connect flow).
 */
@Composable
fun ErrorScreen(onRetry: () -> Unit, onBack: () -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().background(BG).padding(28.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center,
    ) {
        Text("Can't reach your PC", color = INK, fontSize = 20.sp, fontWeight = FontWeight.Bold)
        Spacer(Modifier.height(8.dp))
        Text(
            "Deckster couldn't reach the agent. Make sure it's running on your PC, then try again.",
            color = SUB, fontSize = 14.sp, textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(24.dp))
        Button(
            onClick = onRetry,
            colors = ButtonDefaults.buttonColors(containerColor = BLUE, contentColor = Color(0xFF08121A)),
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Refresh", fontWeight = FontWeight.Bold) }
        Spacer(Modifier.height(10.dp))
        TextButton(onClick = onBack, modifier = Modifier.fillMaxWidth()) {
            Text("Back to connect", color = SUB)
        }
    }
}

@Composable
private fun SectionLabel(text: String) {
    Text(
        text,
        color = SUB,
        fontSize = 11.sp,
        fontWeight = FontWeight.Bold,
        modifier = Modifier.fillMaxWidth().padding(bottom = 6.dp),
    )
}
