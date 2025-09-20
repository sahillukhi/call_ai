import React, { useState, useCallback, useRef, useEffect } from 'react';
import { PhoneOff } from 'lucide-react';
import SlideToCall from './SlideToCall';
import CallTimer from './CallTimer';
import MessageInput from './MessageInput';
import { useToast } from '@/hooks/use-toast';

type CallState = 'idle' | 'connecting' | 'active';

const CallInterface: React.FC = () => {
  const [callState, setCallState] = useState<CallState>('idle');
  const [callStartTime, setCallStartTime] = useState<number>(0);
  const { toast } = useToast();
  
  // WebSocket and audio refs
  const wsRef = useRef<WebSocket | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const inputAudioContextRef = useRef<AudioContext | null>(null);
  const outputAudioContextRef = useRef<AudioContext | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const audioQueueRef = useRef<string[]>([]);
  const isPlayingRef = useRef(false);
  const currentSourceRef = useRef<AudioBufferSourceNode | null>(null);
  const animationFrameRef = useRef<number | null>(null);

  const handleCallStart = useCallback(async () => {
    try {
      setCallState('connecting');
      toast({
        title: "Connecting...",
        description: "Establishing connection",
      });
      
      // Setup audio
      await setupAudio();
      
      // Connect WebSocket - handle ngrok URLs
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      
      // Check if we're running through ngrok (URL contains ngrok.io)
      const isNgrok = window.location.hostname.includes('ngrok.io') || 
                     window.location.hostname.includes('ngrok-free.app') ||
                     window.location.hostname.includes('ngrok.app');
      
      let wsUrl;
      if (isNgrok) {
        // If running through ngrok, connect to the same host (backend serves both frontend and WebSocket)
        const ngrokHost = window.location.host;
        wsUrl = `${protocol}//${ngrokHost}/ws/web-call?agent_id`;
      } else {
        // Local development - connect to localhost:8000
        wsUrl = `${protocol}//localhost:8000/ws/web-call?agent_id`;
      }
      
      const ws = new WebSocket(wsUrl);
      
      ws.onopen = () => {
        wsRef.current = ws;
        setCallState('active');
        setCallStartTime(Date.now());
        toast({
          title: "Call Connected",
          description: "You are now connected",
        });
        
        // Send initial config
        ws.send(JSON.stringify({
          type: 'config',
          sampleRate: 48000,
          inputMode: 'both'
        }));
        
        startAudioProcessing();
        startAudioVisualization();
      };
      
      ws.onmessage = (event) => handleWebSocketMessage(event);
      ws.onerror = () => {
        toast({
          title: "Connection Error",
          description: "Failed to connect to server",
          variant: "destructive",
        });
        setCallState('idle');
      };
      ws.onclose = () => handleCallEnd();
      
    } catch (error) {
      console.error('Error starting call:', error);
      toast({
        title: "Error",
        description: "Failed to start call",
        variant: "destructive",
      });
      setCallState('idle');
    }
  }, [toast]);

  const handleCallEnd = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    cleanup();
    setCallState('idle');
    setCallStartTime(0);
    toast({
      title: "Call Ended",
      description: "The call has been disconnected",
      variant: "destructive",
    });
  }, [toast]);

  const handleSendMessage = useCallback((message: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'text',
        text: message
      }));
      

      
      toast({
        title: "Message Sent",
        description: message,
      });
    } else {
      console.log('Message sent:', message);
      toast({
        title: "Message Sent",
        description: message,
      });
    }
  }, [toast]);

  // WebSocket and audio functions
  const setupAudio = async () => {
    try {
      mediaStreamRef.current = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          sampleRate: 48000,
          channelCount: 1
        }
      });
      
      inputAudioContextRef.current = new (window.AudioContext || (window as any).webkitAudioContext)({
        sampleRate: 48000,
        latencyHint: 'interactive'
      });
      
      analyserRef.current = inputAudioContextRef.current.createAnalyser();
      analyserRef.current.fftSize = 32;
    } catch (error) {
      console.error('Audio setup failed:', error);
      throw error;
    }
  };

  const startAudioProcessing = () => {
    if (!mediaStreamRef.current || !inputAudioContextRef.current || !analyserRef.current) return;
    
    sourceRef.current = inputAudioContextRef.current.createMediaStreamSource(mediaStreamRef.current);
    processorRef.current = inputAudioContextRef.current.createScriptProcessor(2048, 1, 1);
    
    sourceRef.current.connect(analyserRef.current);
    analyserRef.current.connect(processorRef.current);
    processorRef.current.connect(inputAudioContextRef.current.destination);
    
    processorRef.current.onaudioprocess = (e) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      
      const inputData = e.inputBuffer.getChannelData(0);
      const pcm16 = new Int16Array(inputData.length);
      
      for (let i = 0; i < inputData.length; i++) {
        const s = Math.max(-1, Math.min(1, inputData[i]));
        pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
      }
      
      const base64 = btoa(String.fromCharCode(...new Uint8Array(pcm16.buffer)));
      wsRef.current.send(JSON.stringify({
        type: 'audio',
        audio: base64
      }));
    };
  };

  const startAudioVisualization = () => {
    if (!analyserRef.current) return;
    
    const bufferLength = analyserRef.current.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);
    
    const animate = () => {
      if (!analyserRef.current || callState !== 'active') return;
      
      animationFrameRef.current = requestAnimationFrame(animate);
      analyserRef.current!.getByteFrequencyData(dataArray);
      
      // Audio visualization logic removed to keep original design
    };
    
    animate();
  };

  const handleWebSocketMessage = (event: MessageEvent) => {
    try {
      const data = JSON.parse(event.data);
      
      switch (data.type) {
        case 'audio':
          queueAudio(data.audio);
          break;
        case 'transcript':
          // Transcript handling removed to keep original design
          break;
        case 'clear_audio':
          clearAudioQueue();
          break;
        case 'error':
          toast({
            title: "Error",
            description: data.message,
            variant: "destructive",
          });
          break;
      }
    } catch (error) {
      console.error('Error handling message:', error);
    }
  };

  const queueAudio = (audioBase64: string) => {
    audioQueueRef.current.push(audioBase64);
    if (!isPlayingRef.current) {
      playNextAudio();
    }
  };

  const clearAudioQueue = () => {
    console.log('Clearing audio queue due to interruption');
    audioQueueRef.current = [];
    if (currentSourceRef.current) {
      try {
        currentSourceRef.current.stop();
        currentSourceRef.current.disconnect();
      } catch (e) {}
      currentSourceRef.current = null;
    }
    isPlayingRef.current = false;
  };

  const playNextAudio = async () => {
    if (audioQueueRef.current.length === 0) {
      isPlayingRef.current = false;
      return;
    }
    
    isPlayingRef.current = true;
    const audioBase64 = audioQueueRef.current.shift()!;
    
    try {
      if (!outputAudioContextRef.current) {
        outputAudioContextRef.current = new (window.AudioContext || (window as any).webkitAudioContext)();
      }
      
      const binaryString = atob(audioBase64);
      const bytes = new Uint8Array(binaryString.length);
      for (let i = 0; i < binaryString.length; i++) {
        bytes[i] = binaryString.charCodeAt(i);
      }
      
      const int16Array = new Int16Array(bytes.buffer);
      const float32Array = new Float32Array(int16Array.length);
      for (let i = 0; i < int16Array.length; i++) {
        float32Array[i] = int16Array[i] / 32768.0;
      }
      
      const audioBuffer = outputAudioContextRef.current.createBuffer(1, float32Array.length, 48000);
      audioBuffer.getChannelData(0).set(float32Array);
      
      currentSourceRef.current = outputAudioContextRef.current.createBufferSource();
      currentSourceRef.current.buffer = audioBuffer;
      currentSourceRef.current.connect(outputAudioContextRef.current.destination);
      
      currentSourceRef.current.onended = () => {
        currentSourceRef.current = null;
        playNextAudio();
      };
      
      currentSourceRef.current.start();
      
    } catch (error) {
      console.error('Error playing audio:', error);
      currentSourceRef.current = null;
      playNextAudio();
    }
  };

  const cleanup = () => {
    clearAudioQueue();
    
    if (animationFrameRef.current) {
      cancelAnimationFrame(animationFrameRef.current);
      animationFrameRef.current = null;
    }
    
    if (processorRef.current) {
      processorRef.current.disconnect();
      processorRef.current = null;
    }
    
    if (sourceRef.current) {
      sourceRef.current.disconnect();
      sourceRef.current = null;
    }
    
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach(track => track.stop());
      mediaStreamRef.current = null;
    }
    
    if (inputAudioContextRef.current && inputAudioContextRef.current.state !== 'closed') {
      inputAudioContextRef.current.close();
      inputAudioContextRef.current = null;
    }
    
    if (outputAudioContextRef.current && outputAudioContextRef.current.state !== 'closed') {
      outputAudioContextRef.current.close();
      outputAudioContextRef.current = null;
    }
  };

  // Cleanup on unmount
  useEffect(() => {
    return () => cleanup();
  }, []);

  return (
    <div className="animated-background">
      <main className="min-h-screen flex flex-col items-center justify-center p-4 relative z-10">
        {/* Call Timer - Only visible during active call */}
        {callState === 'active' && (
          <div className="absolute top-8 left-1/2 -translate-x-1/2">
            <CallTimer startTime={callStartTime} isActive={callState === 'active'} />
          </div>
        )}

        {/* Main Content Area */}
        <div className="flex flex-col items-center space-y-8 w-full max-w-md">
          {callState === 'idle' && (
            <>
              {/* Pre-call state */}
              <div className="animate-slide-in space-y-8">
                <SlideToCall onCallStart={handleCallStart} />
                <MessageInput 
                  onSendMessage={handleSendMessage}
                  placeholder="Type a message before calling..."
                />
              </div>
            </>
          )}

          {callState === 'connecting' && (
            <div className="animate-slide-in text-center space-y-4">
              <div className="w-16 h-16 mx-auto">
                <div className="w-full h-full rounded-full bg-gradient-primary animate-pulse-glow"></div>
              </div>
              <p className="text-lg text-muted-foreground">Connecting...</p>
            </div>
          )}

          {callState === 'active' && (
            <div className="animate-slide-in flex flex-col items-center space-y-8">
              {/* Active call content */}
              <div className="text-center space-y-2">
                <h2 className="text-xl font-medium text-foreground">Call Active</h2>
                <p className="text-muted-foreground">Connected and ready</p>
              </div>



              {/* End Call Button */}
              <button
                onClick={handleCallEnd}
                className="end-call-button"
                aria-label="End call"
              >
                <PhoneOff size={32} />
              </button>

              {/* Message input during call */}
              <MessageInput 
                onSendMessage={handleSendMessage}
                placeholder="Send a message during call..."
              />


            </div>
          )}
        </div>

        {/* Status indicator */}
        <div className="absolute bottom-8 left-1/2 -translate-x-1/2">
          <div className={`px-4 py-2 rounded-full text-sm font-medium transition-all ${
            callState === 'idle' 
              ? 'bg-muted/30 text-muted-foreground' 
              : callState === 'connecting'
              ? 'bg-warning/20 text-warning'
              : 'bg-success/20 text-success'
          }`}>
            {callState === 'idle' && 'Ready to connect'}
            {callState === 'connecting' && 'Establishing connection...'}
            {callState === 'active' && 'Call in progress'}
          </div>
        </div>
      </main>
    </div>
  );
};

export default CallInterface;