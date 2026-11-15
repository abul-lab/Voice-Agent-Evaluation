# Generates the speech clips for the barge-in lab with the Windows speech synthesiser (no network, no keys).
# Each clip is spoken by two voices, and a manifest records what each clip is (interrupt or backchannel).
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tools/barge_in/make_clips.ps1 -OutDir results/barge-in/clips
param([string]$OutDir = "results/barge-in/clips")
Add-Type -AssemblyName System.Speech
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$dir = (Resolve-Path $OutDir).Path

$agentText = 'Hi, welcome to Eliza Voice Shopping. I can help you find gear, add it to your cart, check out, track an order or request a refund. You can also browse tents, sleeping bags, backpacks and stoves. What would you like to do today?'

# Caller phrases. An interrupt is a real request or correction; a backchannel is an acknowledgement the agent should talk through.
$interrupts = @(
  'Wait, I want the Ridgeline tent instead.',
  'Actually, can you check my order first?',
  'No, not that one, the blue one.',
  'Hold on, I need to change the quantity.',
  'Sorry, can I ask about a refund?',
  'Stop, that is not what I asked.'
)
$backchannels = @(
  'Mm-hmm.',
  'Yeah, right.',
  'Okay.',
  'Uh-huh.',
  'Right, okay.',
  'I see.'
)
$voices = @('Microsoft David Desktop', 'Microsoft Zira Desktop')

$entries = @()
function Make($name, $voice, $text, $rate) {
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $s.SelectVoice($voice)
  $s.Rate = $rate
  $s.SetOutputToWaveFile((Join-Path $dir $name))
  $s.Speak($text)
  $s.Dispose()
}

Make 'agent_reply.wav' 'Microsoft David Desktop' $agentText 0
$entries += [pscustomobject]@{ clip = 'agent_reply.wav'; kind = 'agent'; expect = ''; voice = 'David'; text = $agentText }

$v = 0
foreach ($voice in $voices) {
  $short = ($voice -split ' ')[1]
  $i = 0
  foreach ($text in $interrupts) {
    $i++
    $name = "interrupt_${short}_$i.wav".ToLower()
    Make $name $voice $text 0
    $entries += [pscustomobject]@{ clip = $name; kind = 'interrupt'; expect = 'stop'; voice = $short; text = $text }
  }
  $b = 0
  foreach ($text in $backchannels) {
    $b++
    $name = "backchannel_${short}_$b.wav".ToLower()
    # Backchannels are said quickly, as people do; the rate is 2 (0 is normal speed).
    Make $name $voice $text 2
    $entries += [pscustomobject]@{ clip = $name; kind = 'backchannel'; expect = 'continue'; voice = $short; text = $text }
  }
}

# Demo clip for the video: a caller says "Um, nah." then pauses, then gives a new request.
$demoSsml = '<speak version="1.0" xml:lang="en-US">Um, nah.<break time="900ms"/>Show me the Summit dome instead.</speak>'
$demo = New-Object System.Speech.Synthesis.SpeechSynthesizer
$demo.SelectVoice('Microsoft David Desktop')
$demo.SetOutputToWaveFile((Join-Path $dir 'interrupt_um_nah.wav'))
$demo.SpeakSsml($demoSsml)
$demo.Dispose()
$entries += [pscustomobject]@{ clip = 'interrupt_um_nah.wav'; kind = 'interrupt'; expect = 'stop'; voice = 'David'; text = 'Um, nah. Show me the Summit dome instead.' }

$entries | ConvertTo-Json -Depth 3 | Set-Content -Path (Join-Path $dir 'manifest.json') -Encoding utf8
"wrote $($entries.Count) clips and manifest.json to $dir"
