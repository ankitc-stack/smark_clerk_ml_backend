# Wake Word Integration — Frontend Developer Guide

## Overview

The backend exposes a WebSocket endpoint for always-on wake word detection.

- **"Start Clerk"** → mic activates (system enters LISTENING state)
- **"Over Clerk"** → mic deactivates (system returns to IDLE)

The browser streams raw audio continuously to the server in small chunks.
The server does the detection and sends events back.

---

## Backend Endpoint

```
ws://YOUR_SERVER_IP:8000/ws/wake-word
```

**Browser sends:** raw `Int16Array` PCM audio (16 kHz, mono, 100 ms chunks = 3 200 bytes each)

**Server sends JSON:**

| Event | Meaning |
|---|---|
| `{"event": "activate", "score": 0.87}` | "Start Clerk" detected — activate mic |
| `{"event": "deactivate", "score": 0.91}` | "Over Clerk" detected — deactivate mic |
| `{"event": "model_unavailable", "message": "..."}` | Models not trained yet (dev environment) |

---

## npm Packages to Install

```bash
npm install @ricky0123/vad-web      # end-of-utterance detection (MIT, offline ONNX)
```

No package needed for wake word — that runs on the server.

---

## Voice State Machine

```
IDLE
  │  "Start Clerk" detected  OR  mic button clicked
  ▼
LISTENING         ← green pulsing mic icon shown
  │  VAD detects speech start
  ▼
RECORDING         ← recording indicator shown
  │  VAD detects 1.5 s silence (end of utterance)
  ▼
PROCESSING        ← spinner shown
  │  backend returns result
  ▼
LISTENING         ← stays here (chain commands without saying "Start Clerk" again)

From LISTENING:
  "Over Clerk" detected  OR  mic button clicked  →  IDLE
```

---

## Zustand Store (voice state)

```typescript
// store/voiceStore.ts
import { create } from 'zustand'

export type VoiceState = 'IDLE' | 'LISTENING' | 'RECORDING' | 'PROCESSING'

interface VoiceStore {
  state: VoiceState
  lastTranscript: string
  setState: (s: VoiceState) => void
  setTranscript: (t: string) => void
}

export const useVoiceStore = create<VoiceStore>((set) => ({
  state: 'IDLE',
  lastTranscript: '',
  setState:    (state)      => set({ state }),
  setTranscript: (lastTranscript) => set({ lastTranscript }),
}))
```

---

## Component 1 — WakeWordDetector (always mounted, invisible)

This component runs in the background the entire time the document page is open.

```typescript
// components/WakeWordDetector.tsx
import { useEffect, useRef } from 'react'
import { useVoiceStore } from '../store/voiceStore'

const SAMPLE_RATE   = 16000
const CHUNK_MS      = 100                           // send 100 ms chunks
const CHUNK_SAMPLES = (SAMPLE_RATE * CHUNK_MS) / 1000  // 1600 samples

export function WakeWordDetector() {
  const setState    = useVoiceStore((s) => s.setState)
  const voiceState  = useVoiceStore((s) => s.state)
  const wsRef       = useRef<WebSocket | null>(null)
  const ctxRef      = useRef<AudioContext | null>(null)

  useEffect(() => {
    let ws: WebSocket
    let audioCtx: AudioContext
    let source: MediaStreamAudioSourceNode
    let processor: ScriptProcessorNode
    let stream: MediaStream

    async function start() {
      // 1. Open WebSocket to backend
      ws = new WebSocket(`ws://${window.location.hostname}:8000/ws/wake-word`)
      wsRef.current = ws

      ws.onmessage = (e) => {
        const data = JSON.parse(e.data)
        if (data.event === 'activate') {
          setState('LISTENING')
        } else if (data.event === 'deactivate') {
          setState('IDLE')
        } else if (data.event === 'model_unavailable') {
          console.warn('Wake word models not trained yet:', data.message)
        }
      }

      ws.onerror = () => console.error('Wake word WebSocket error')
      ws.onclose = () => console.log('Wake word WebSocket closed')

      // 2. Capture mic at 16 kHz
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { sampleRate: SAMPLE_RATE, channelCount: 1, echoCancellation: true },
      })

      audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE })
      ctxRef.current = audioCtx
      source = audioCtx.createMediaStreamSource(stream)

      // ScriptProcessorNode sends 100ms PCM chunks to server
      processor = audioCtx.createScriptProcessor(CHUNK_SAMPLES, 1, 1)
      processor.onaudioprocess = (e) => {
        if (ws.readyState !== WebSocket.OPEN) return
        const float32 = e.inputBuffer.getChannelData(0)
        const int16   = new Int16Array(float32.length)
        for (let i = 0; i < float32.length; i++) {
          int16[i] = Math.max(-32768, Math.min(32767, float32[i] * 32768))
        }
        ws.send(int16.buffer)
      }

      source.connect(processor)
      processor.connect(audioCtx.destination)
    }

    start().catch(console.error)

    return () => {
      ws?.close()
      processor?.disconnect()
      source?.disconnect()
      audioCtx?.close()
      stream?.getTracks().forEach((t) => t.stop())
    }
  }, [])

  return null  // invisible — no UI output
}
```

**Mount this once at the top of your Document page:**
```tsx
// pages/DocumentPage.tsx
import { WakeWordDetector } from '../components/WakeWordDetector'

export function DocumentPage() {
  return (
    <>
      <WakeWordDetector />   {/* always-on, invisible */}
      {/* ... rest of page */}
    </>
  )
}
```

---

## Component 2 — VoiceCommandBar (visible UI + VAD recording)

This handles: LISTENING → RECORDING → PROCESSING and sends audio to the command API.

```typescript
// components/VoiceCommandBar.tsx
import { useEffect, useRef } from 'react'
import { useMicVAD }         from '@ricky0123/vad-web'
import { useVoiceStore }     from '../store/voiceStore'

interface Props {
  docId:   string
  version: number
  onResult: (updates: any[]) => void
}

export function VoiceCommandBar({ docId, version, onResult }: Props) {
  const { state, setState, setTranscript } = useVoiceStore()

  // VAD — only active while LISTENING or RECORDING
  const vad = useMicVAD({
    startOnLoad: false,
    onSpeechStart: () => {
      if (state === 'LISTENING') setState('RECORDING')
    },
    onSpeechEnd: async (audio: Float32Array) => {
      setState('PROCESSING')
      const b64 = float32ToBase64Wav(audio, 16000)
      try {
        const res = await fetch(`/documents/${docId}/command`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            version,
            context: {},
            input: { type: 'voice', audio_base64: b64 },
          }),
        })
        const data = await res.json()
        if (data.status === 'applied') {
          setTranscript(data.transcript || '')
          onResult(data.updates || [])
        }
      } catch (err) {
        console.error('Command failed:', err)
      } finally {
        setState('LISTENING')  // stay in LISTENING for next command
      }
    },
    positiveSpeechThreshold: 0.8,
    minSpeechFrames: 3,
  })

  // Start/stop VAD based on voice state
  useEffect(() => {
    if (state === 'LISTENING' || state === 'RECORDING') {
      vad.start()
    } else {
      vad.pause()
    }
  }, [state])

  return (
    <div className="voice-command-bar">
      <MicButton
        state={state}
        onClick={() => setState(state === 'IDLE' ? 'LISTENING' : 'IDLE')}
      />
      <StatusText state={state} />
    </div>
  )
}

// ---------- UI sub-components ----------

function MicButton({ state, onClick }: { state: string; onClick: () => void }) {
  const icons: Record<string, string> = {
    IDLE:       '🎙️',
    LISTENING:  '🟢',
    RECORDING:  '🔴',
    PROCESSING: '⏳',
  }
  return (
    <button
      onClick={onClick}
      className={`mic-btn mic-btn--${state.toLowerCase()}`}
      title={state === 'IDLE' ? 'Click or say "Start Clerk"' : 'Click or say "Over Clerk"'}
    >
      {icons[state] ?? '🎙️'}
    </button>
  )
}

function StatusText({ state }: { state: string }) {
  const labels: Record<string, string> = {
    IDLE:       'Say "Start Clerk" or click mic',
    LISTENING:  'Listening… say your command',
    RECORDING:  'Recording…',
    PROCESSING: 'Processing…',
  }
  return <span className="voice-status">{labels[state] ?? ''}</span>
}

// ---------- Audio helpers ----------

function float32ToBase64Wav(samples: Float32Array, sampleRate: number): string {
  const int16 = new Int16Array(samples.length)
  for (let i = 0; i < samples.length; i++) {
    int16[i] = Math.max(-32768, Math.min(32767, samples[i] * 32768))
  }
  const wavBuffer = buildWav(int16, sampleRate)
  const bytes     = new Uint8Array(wavBuffer)
  let binary      = ''
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i])
  return btoa(binary)
}

function buildWav(pcm: Int16Array, sampleRate: number): ArrayBuffer {
  const dataLength = pcm.length * 2
  const buf        = new ArrayBuffer(44 + dataLength)
  const view       = new DataView(buf)
  const write      = (off: number, s: string) =>
    [...s].forEach((c, i) => view.setUint8(off + i, c.charCodeAt(0)))
  write(0,  'RIFF')
  view.setUint32( 4, 36 + dataLength, true)
  write(8,  'WAVE')
  write(12, 'fmt ')
  view.setUint32(16, 16, true)
  view.setUint16(20, 1,  true)   // PCM
  view.setUint16(22, 1,  true)   // mono
  view.setUint32(24, sampleRate, true)
  view.setUint32(28, sampleRate * 2, true)
  view.setUint16(32, 2,  true)
  view.setUint16(34, 16, true)
  write(36, 'data')
  view.setUint32(40, dataLength, true)
  for (let i = 0; i < pcm.length; i++) view.setInt16(44 + i * 2, pcm[i], true)
  return buf
}
```

---

## CSS — Mic Button Pulse Animation

```css
/* styles/voice.css */

.mic-btn {
  width: 48px; height: 48px;
  border-radius: 50%;
  border: none;
  font-size: 22px;
  cursor: pointer;
  transition: background 0.2s;
}

.mic-btn--idle       { background: #e0e0e0; }
.mic-btn--processing { background: #f5f5f5; opacity: 0.7; cursor: not-allowed; }
.mic-btn--recording  { background: #ffcdd2; }

/* Green pulsing ring when LISTENING */
.mic-btn--listening {
  background: #c8e6c9;
  animation: pulse-ring 1.2s ease-in-out infinite;
}

@keyframes pulse-ring {
  0%   { box-shadow: 0 0 0 0 rgba(76, 175, 80, 0.5); }
  70%  { box-shadow: 0 0 0 12px rgba(76, 175, 80, 0); }
  100% { box-shadow: 0 0 0 0 rgba(76, 175, 80, 0); }
}

.voice-status {
  font-size: 13px;
  color: #555;
  margin-left: 10px;
}
```

---

## Complete Page Assembly

```tsx
// pages/DocumentPage.tsx
import { WakeWordDetector } from '../components/WakeWordDetector'
import { VoiceCommandBar }  from '../components/VoiceCommandBar'

export function DocumentPage({ docId, version }: { docId: string; version: number }) {
  function handleCommandResult(updates: any[]) {
    // Apply section updates from the command response
    // updates is the same array returned by POST /documents/{id}/command
    // Loop through and update your Lexical editor states
    updates.forEach((u) => {
      if (u.op === 'update_section' && u.new_lexical_state) {
        // find the section editor by u.section_id and call setState
      }
      if (u.op === 'insert')  { /* add new SectionEditor */ }
      if (u.op === 'delete')  { /* remove SectionEditor */ }
    })
  }

  return (
    <div className="document-page">
      {/* Always-on wake word listener — no visible output */}
      <WakeWordDetector />

      {/* Toolbar + mic UI */}
      <VoiceCommandBar
        docId={docId}
        version={version}
        onResult={handleCommandResult}
      />

      {/* ... Section editors, toolbar, etc. */}
    </div>
  )
}
```

---

## Backend Setup (for backend dev)

Before the WebSocket endpoint will work, train the wake word models once:

```bash
# Install training dependencies
pip install openwakeword[training] pyttsx3

# Train both models (~15–30 min, CPU)
python scripts/train_wake_words.py

# Restart the server
docker restart smark_clerk_ml_backend-main-ml-pipeline-1
```

Models are saved to `data/wake_word_models/` and loaded automatically on first WebSocket connection.

**To improve accuracy with real recordings** (recommended for production):
1. Record 20–50 audio clips of officers saying "Start Clerk" (16 kHz, mono WAV)
2. Place them in `data/wake_word_training/start_clerk/positive/`
3. Do the same for "Over Clerk"
4. Delete old `.onnx` files and re-run the training script

---

## Quick Test (without full UI)

Test the WebSocket directly from browser console:

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/wake-word')
ws.onmessage = (e) => console.log('WS event:', JSON.parse(e.data))
ws.onopen = () => console.log('connected')
// ws.send(new Int16Array(1600).buffer)  // send silence chunk to verify connection
```

---

## Checklist for Frontend Dev

- [ ] Install `@ricky0123/vad-web`
- [ ] Copy `WakeWordDetector.tsx` — mount once at DocumentPage root
- [ ] Copy `VoiceCommandBar.tsx` — include in DocumentPage toolbar area
- [ ] Create `store/voiceStore.ts` (Zustand)
- [ ] Add CSS pulse animation for `mic-btn--listening`
- [ ] Wire `onResult` in `VoiceCommandBar` to update Lexical section editors
- [ ] Test: open document → say "Start Clerk" → mic activates → give a command → command executes → say "Over Clerk" → mic deactivates
- [ ] Test: mic button click also works as fallback (no wake word needed)

---

## Notes

- `WakeWordDetector` streams audio **continuously** even in IDLE state — this is intentional (needed to detect "Start Clerk"). The audio goes only to the backend WebSocket and is not stored anywhere.
- The `VoiceCommandBar` VAD only activates in LISTENING/RECORDING state — no audio is sent to the command endpoint until the system is activated.
- Both components can share the same mic stream if needed (AudioContext can have multiple processors). For simplicity, they each open their own stream.
- On mobile browsers, `AudioContext` may require a user gesture to start — trigger `WakeWordDetector` initialization on first tap/click.
