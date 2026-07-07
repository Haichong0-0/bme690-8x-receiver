using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

/// <summary>
/// WebSocket client that dials out to the Python host and receives smell packets
/// (plan.md §5). The HoloLens is always the client; only the URL changes between
/// Approach A (ws:// LAN) and Approach B (wss:// relay).
///
/// THREADING RULE (plan.md §5): frames are received on a background Task and
/// pushed into a thread-safe queue. NO Unity API is touched off the main thread.
/// <see cref="Update"/> drains the queue on the main thread and exposes the
/// freshest packet as <see cref="LatestPacket"/> so any number of downstream
/// consumers (visualiser, debug HUD, ...) can read it without fighting over the
/// queue themselves.
/// </summary>
public class SmellReceiver : MonoBehaviour
{
    [Tooltip("Approach A (LAN): ws://<host-ip>:8765   |   " +
             "Approach B (relay): wss://<relay-domain>\n" +
             "Use ws://127.0.0.1:8765 to test against ws_publisher.py on this PC.\n" +
             "Overridden at runtime if server_url.txt exists in persistentDataPath " +
             "(see OverrideFileName) — lets you repoint at a new host without " +
             "rebuilding when you switch networks.")]
    public string serverUrl = "ws://127.0.0.1:8765";

    /// <summary>
    /// Optional runtime override, read once at startup from
    /// <c>&lt;Application.persistentDataPath&gt;/server_url.txt</c> — on a deployed
    /// UWP app that's the package's LocalState folder, reachable over Device
    /// Portal's File Explorer (pick this app, browse to LocalState) with no
    /// rebuild/redeploy needed. One line of text: the new ws:// or wss:// URL.
    /// </summary>
    public const string OverrideFileName = "server_url.txt";

    [Tooltip("Seconds to wait before reconnecting after a dropped connection.")]
    public float reconnectDelaySeconds = 2f;

    /// <summary>Raw packets waiting for the main thread to consume. Background-thread writer, main-thread reader.</summary>
    public readonly ConcurrentQueue<SmellPacket> Packets = new ConcurrentQueue<SmellPacket>();

    /// <summary>True while a socket is open (for HUD/debug).</summary>
    public bool IsConnected { get; private set; }

    /// <summary>The most recent packet, by seq (plan.md §3: stale/out-of-order frames are dropped).</summary>
    public SmellPacket LatestPacket { get; private set; }

    /// <summary>True once at least one valid packet has been received.</summary>
    public bool HasReceivedData { get; private set; }

    /// <summary><see cref="Time.realtimeSinceStartup"/> when <see cref="LatestPacket"/> was last updated.</summary>
    public float LastPacketRealtime { get; private set; } = -999f;

    /// <summary>Packets accepted per second, updated once a second (debug/HUD use).</summary>
    public int PacketsPerSecond { get; private set; }

    private int _packetsThisWindow;
    private float _rateWindowStart;

    private CancellationTokenSource _cts;

    private void Awake()
    {
        ApplyUrlOverrideIfPresent();
    }

    private void ApplyUrlOverrideIfPresent()
    {
        try
        {
            string overridePath = Path.Combine(Application.persistentDataPath, OverrideFileName);
            if (!File.Exists(overridePath)) return;

            string overrideUrl = File.ReadAllText(overridePath).Trim();
            if (string.IsNullOrEmpty(overrideUrl)) return;

            Debug.Log($"[SmellReceiver] serverUrl overridden from {overridePath}: " +
                      $"{serverUrl} -> {overrideUrl}");
            serverUrl = overrideUrl;
        }
        catch (Exception e)
        {
            Debug.LogWarning($"[SmellReceiver] failed to read URL override: {e.Message}");
        }
    }

    private void OnEnable()
    {
        _cts = new CancellationTokenSource();
        _ = ReceiveLoop(_cts.Token);   // fire-and-forget background task
    }

    private void OnDisable()
    {
        _cts?.Cancel();
        _cts?.Dispose();
        _cts = null;
        IsConnected = false;
    }

    private void Update()
    {
        // Single drain point: everything downstream reads LatestPacket instead of
        // touching the queue, so multiple consumers never race over TryDequeue.
        while (Packets.TryDequeue(out var p))
        {
            if (!HasReceivedData || p.seq > LatestPacket.seq)
            {
                LatestPacket = p;
                HasReceivedData = true;
            }
            LastPacketRealtime = Time.realtimeSinceStartup;
            _packetsThisWindow++;
        }

        if (Time.realtimeSinceStartup - _rateWindowStart >= 1f)
        {
            PacketsPerSecond = _packetsThisWindow;
            _packetsThisWindow = 0;
            _rateWindowStart = Time.realtimeSinceStartup;
        }
    }

    private async Task ReceiveLoop(CancellationToken token)
    {
        var buffer = new byte[8192];

        // Outer loop: reconnect forever until the component is disabled.
        while (!token.IsCancellationRequested)
        {
            using (var ws = new ClientWebSocket())
            {
                try
                {
                    await ws.ConnectAsync(new Uri(serverUrl), token);
                    IsConnected = true;
                    // A (re)connect may be to a freshly restarted publisher whose
                    // seq counter starts back at 0 — drop the old high-water mark
                    // so the next packet is accepted regardless of its seq value.
                    HasReceivedData = false;
                    Debug.Log($"[SmellReceiver] connected to {serverUrl}");

                    // Inner loop: read text frames until the socket closes.
                    while (ws.State == WebSocketState.Open && !token.IsCancellationRequested)
                    {
                        var sb = new StringBuilder();
                        WebSocketReceiveResult result;
                        do
                        {
                            result = await ws.ReceiveAsync(new ArraySegment<byte>(buffer), token);
                            if (result.MessageType == WebSocketMessageType.Close)
                            {
                                await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "", token);
                                break;
                            }
                            sb.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
                        } while (!result.EndOfMessage);

                        if (sb.Length > 0 && SmellPacket.TryParse(sb.ToString(), out var packet))
                            Packets.Enqueue(packet);   // hand off to main thread
                    }
                }
                catch (OperationCanceledException)
                {
                    // normal shutdown
                }
                catch (Exception e)
                {
                    Debug.LogWarning($"[SmellReceiver] connection error: {e.Message}");
                }
                finally
                {
                    // Guard against a stale loop (already cancelled by a newer
                    // OnEnable/OnDisable cycle) clobbering a fresher loop's
                    // IsConnected = true after it has already reconnected.
                    if (!token.IsCancellationRequested)
                        IsConnected = false;
                }
            }

            if (token.IsCancellationRequested) break;

            // Wait, then retry the connection (plan.md §7 milestone 9: auto-reconnect).
            try { await Task.Delay(TimeSpan.FromSeconds(reconnectDelaySeconds), token); }
            catch (OperationCanceledException) { break; }
        }
    }
}
