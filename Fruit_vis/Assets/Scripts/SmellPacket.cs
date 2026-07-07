using System;
using UnityEngine;

/// <summary>
/// The single host→HoloLens message (plan.md §3). Field names MUST match the
/// JSON keys emitted by ws_publisher.py exactly, so Unity's JsonUtility can map
/// them. This is the locked interface between the Python host and this app.
/// </summary>
[Serializable]
public struct SmellPacket
{
    public double timestamp;          // host epoch seconds when inference ran
    public string odour;              // "lemon" | "grapefruit" | "lavender"
    public float odour_confidence;    // 0..1 classifier confidence
    public float intensity;           // 0..1 normalised magnitude → drives visual
    public int seq;                   // monotonic counter → drop stale/out-of-order

    /// <summary>Parse one JSON text frame. Returns false on malformed input.</summary>
    public static bool TryParse(string json, out SmellPacket packet)
    {
        try
        {
            packet = JsonUtility.FromJson<SmellPacket>(json);
            // odour is null if the JSON didn't contain the expected fields.
            return packet.odour != null;
        }
        catch
        {
            packet = default;
            return false;
        }
    }
}
