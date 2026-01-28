/**
 * WebRTC Audio Interceptor
 *
 * This script intercepts RTCPeerConnection to capture incoming audio streams,
 * mixes them using AudioContext, and records via MediaRecorder.
 *
 * Must be injected before page load via page.add_init_script()
 */

(function() {
    'use strict';

    // Store for captured streams and recorder state
    window.__rtcInterceptor = {
        audioContext: null,
        mixerDestination: null,
        mediaRecorder: null,
        recordedChunks: [],
        isRecording: false,
        connectedTracks: new Map(), // track.id -> {track, source}
        peerConnections: [],
        debug: true,
    };

    const interceptor = window.__rtcInterceptor;

    function log(...args) {
        if (interceptor.debug) {
            console.log('[RTC Interceptor]', ...args);
        }
    }

    // Initialize AudioContext for mixing streams
    function initAudioContext() {
        if (!interceptor.audioContext) {
            interceptor.audioContext = new AudioContext();
            interceptor.mixerDestination = interceptor.audioContext.createMediaStreamDestination();
            log('AudioContext initialized, state:', interceptor.audioContext.state);
        }

        // Always try to resume
        if (interceptor.audioContext.state === 'suspended') {
            interceptor.audioContext.resume().then(() => {
                log('AudioContext resumed');
            }).catch(e => {
                log('AudioContext resume failed:', e);
            });
        }

        return interceptor.audioContext;
    }

    // Connect an audio track to the mixer
    function connectTrackToMixer(track, source) {
        if (interceptor.connectedTracks.has(track.id)) {
            log('Track already connected:', track.id);
            return;
        }

        const ctx = initAudioContext();

        try {
            const stream = new MediaStream([track]);
            const audioSource = ctx.createMediaStreamSource(stream);
            audioSource.connect(interceptor.mixerDestination);

            interceptor.connectedTracks.set(track.id, {
                track: track,
                source: audioSource,
                connectedAt: Date.now()
            });

            log('Audio track connected:', track.id, 'from:', source, 'total tracks:', interceptor.connectedTracks.size);

            // Handle track ending
            track.addEventListener('ended', () => {
                log('Audio track ended:', track.id);
                interceptor.connectedTracks.delete(track.id);
            });

            track.addEventListener('mute', () => {
                log('Audio track muted:', track.id);
            });

            track.addEventListener('unmute', () => {
                log('Audio track unmuted:', track.id);
            });

        } catch (e) {
            log('Error connecting track:', e);
        }
    }

    // Override RTCPeerConnection to intercept audio tracks
    const OriginalRTCPeerConnection = window.RTCPeerConnection;

    window.RTCPeerConnection = function(...args) {
        log('New RTCPeerConnection created with config:', JSON.stringify(args[0]));

        const pc = new OriginalRTCPeerConnection(...args);
        interceptor.peerConnections.push(pc);

        // Listen for track events
        pc.addEventListener('track', (event) => {
            log('Track event received:', event.track.kind, event.track.id, 'readyState:', event.track.readyState);

            if (event.track.kind === 'audio') {
                connectTrackToMixer(event.track, 'addEventListener');
            }

            // Also check streams
            if (event.streams && event.streams.length > 0) {
                event.streams.forEach((stream, idx) => {
                    log('Stream', idx, 'has', stream.getAudioTracks().length, 'audio tracks');
                    stream.getAudioTracks().forEach(audioTrack => {
                        connectTrackToMixer(audioTrack, 'stream');
                    });
                });
            }
        });

        // Also intercept addTrack on remote streams
        const originalAddTrack = pc.addTrack;
        if (originalAddTrack) {
            pc.addTrack = function(track, ...streams) {
                log('addTrack called:', track.kind, track.id);
                return originalAddTrack.apply(this, [track, ...streams]);
            };
        }

        // Monitor connection state
        pc.addEventListener('connectionstatechange', () => {
            log('Connection state changed:', pc.connectionState);
        });

        pc.addEventListener('iceconnectionstatechange', () => {
            log('ICE connection state:', pc.iceConnectionState);
        });

        return pc;
    };

    // Copy static properties and prototype
    Object.setPrototypeOf(window.RTCPeerConnection, OriginalRTCPeerConnection);
    Object.setPrototypeOf(window.RTCPeerConnection.prototype, OriginalRTCPeerConnection.prototype);

    // Copy static methods
    Object.getOwnPropertyNames(OriginalRTCPeerConnection).forEach(key => {
        if (key !== 'prototype' && key !== 'length' && key !== 'name') {
            try {
                window.RTCPeerConnection[key] = OriginalRTCPeerConnection[key];
            } catch (e) {}
        }
    });

    // Start recording mixed audio
    window.__rtcStartRecording = function() {
        if (interceptor.isRecording) {
            log('Already recording');
            return false;
        }

        initAudioContext();

        // Resume AudioContext
        if (interceptor.audioContext.state !== 'running') {
            interceptor.audioContext.resume();
        }

        interceptor.recordedChunks = [];

        // Determine supported mime type
        let mimeType = 'audio/webm;codecs=opus';
        if (!MediaRecorder.isTypeSupported(mimeType)) {
            mimeType = 'audio/webm';
            if (!MediaRecorder.isTypeSupported(mimeType)) {
                mimeType = 'audio/ogg';
            }
        }
        log('Using mimeType:', mimeType);

        try {
            interceptor.mediaRecorder = new MediaRecorder(
                interceptor.mixerDestination.stream,
                { mimeType: mimeType }
            );
        } catch (e) {
            log('MediaRecorder creation failed:', e);
            return false;
        }

        interceptor.mediaRecorder.ondataavailable = (event) => {
            if (event.data.size > 0) {
                interceptor.recordedChunks.push(event.data);
                log('Chunk recorded, size:', event.data.size, 'total chunks:', interceptor.recordedChunks.length);
            }
        };

        interceptor.mediaRecorder.onerror = (event) => {
            log('MediaRecorder error:', event.error);
        };

        interceptor.mediaRecorder.start(1000); // Capture every second
        interceptor.isRecording = true;

        log('Recording started, AudioContext state:', interceptor.audioContext.state);
        return true;
    };

    // Stop recording and return audio data
    window.__rtcStopRecording = function() {
        return new Promise((resolve) => {
            if (!interceptor.isRecording || !interceptor.mediaRecorder) {
                log('Not recording');
                resolve(null);
                return;
            }

            interceptor.mediaRecorder.onstop = () => {
                interceptor.isRecording = false;
                log('Recording stopped, chunks:', interceptor.recordedChunks.length);

                if (interceptor.recordedChunks.length === 0) {
                    log('No chunks recorded!');
                    resolve(null);
                    return;
                }

                const blob = new Blob(interceptor.recordedChunks, { type: 'audio/webm' });
                log('Blob created, size:', blob.size);

                // Convert to base64
                const reader = new FileReader();
                reader.onloadend = () => {
                    const base64 = reader.result.split(',')[1];
                    resolve({
                        data: base64,
                        mimeType: 'audio/webm',
                        size: blob.size,
                        duration: interceptor.recordedChunks.length
                    });
                };
                reader.onerror = (e) => {
                    log('FileReader error:', e);
                    resolve(null);
                };
                reader.readAsDataURL(blob);
            };

            interceptor.mediaRecorder.stop();
        });
    };

    // Get recording status
    window.__rtcGetStatus = function() {
        return {
            isRecording: interceptor.isRecording,
            tracksConnected: interceptor.connectedTracks.size,
            trackIds: Array.from(interceptor.connectedTracks.keys()),
            peerConnections: interceptor.peerConnections.length,
            chunksRecorded: interceptor.recordedChunks.length,
            audioContextState: interceptor.audioContext?.state || 'not initialized'
        };
    };

    // Check if any audio is being received
    window.__rtcHasAudio = function() {
        return interceptor.connectedTracks.size > 0;
    };

    // Force connect any existing audio elements on the page
    window.__rtcCapturePageAudio = function() {
        log('Attempting to capture page audio elements...');
        const audioElements = document.querySelectorAll('audio, video');
        log('Found', audioElements.length, 'audio/video elements');

        audioElements.forEach((el, idx) => {
            try {
                if (el.srcObject) {
                    const tracks = el.srcObject.getAudioTracks();
                    log('Element', idx, 'has', tracks.length, 'audio tracks');
                    tracks.forEach(track => {
                        connectTrackToMixer(track, 'pageElement');
                    });
                }
            } catch (e) {
                log('Error processing element:', e);
            }
        });
    };

    log('Loaded and ready');
})();
