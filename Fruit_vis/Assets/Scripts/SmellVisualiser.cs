using System;
using UnityEngine;

/// <summary>
/// One odour's visual identity. Assign <see cref="prefab"/> to show a real model
/// (e.g. a fruit imported from HololensFruit's asset pack); leave it empty to get
/// a coloured placeholder sphere instead, so the app still runs before real
/// models exist.
/// </summary>
[Serializable]
public struct OdourVisual
{
    [Tooltip("Must match the packet's \"odour\" string exactly (plan.md §3).")]
    public string odour;
    [Tooltip("Optional. If unset, this odour recolours the shared placeholder mesh instead.")]
    public GameObject prefab;
    [Tooltip("Placeholder colour, used only when prefab is unset.")]
    public Color placeholderColor;
}

/// <summary>
/// Turns incoming smell packets into a visual (plan.md §6). Reads
/// <see cref="SmellReceiver.LatestPacket"/> each frame (already deduped/dropped
/// for staleness by the receiver) and drives the active odour's object.
///
/// Architecture adapted from the old HololensFruit/ModelController.cs pattern:
/// one instance per odour with a real <see cref="OdourVisual.prefab"/>, spawned
/// once and toggled active/inactive on odour change. Odours with no prefab
/// assigned instead share a single placeholder mesh that gets recoloured in
/// place when the active odour changes among them — there's no real model to
/// swap between yet, so "changing odour" reads as one mesh changing colour
/// (and its scale carries over smoothly, rather than resetting on every
/// switch). Intensity (0..1) drives scale on whichever instance is active.
/// </summary>
public class SmellVisualiser : MonoBehaviour
{
    [Tooltip("The receiver to read the latest packet from. Auto-filled if on the same object.")]
    public SmellReceiver receiver;

    // plan.md declares lemon/grapefruit/lavender, but the deployed classifier
    // (ML/train.py, see ML/models/metadata.json) was trained on lemon/grapefruit/
    // sorange -- lavender was never captured, sorange was. Packets carrying an
    // odour string with no matching entry here are treated the same as
    // unconfident ones: nothing is accepted, so after idleTimeoutSeconds the
    // visual decays to nothing (see Update). Keeping "lavender" too in case a
    // future capture actually adds it -- harmless, since it's simply never
    // matched by any real packet today.
    [Header("Per-odour visuals (trained model: lemon, grapefruit, sorange)")]
    public OdourVisual[] odourVisuals = new[]
    {
        new OdourVisual { odour = "lemon",      placeholderColor = new Color(1.00f, 0.90f, 0.20f) },
        new OdourVisual { odour = "grapefruit", placeholderColor = new Color(1.00f, 0.45f, 0.40f) },
        new OdourVisual { odour = "sorange",    placeholderColor = new Color(1.00f, 0.60f, 0.15f) },
        new OdourVisual { odour = "lavender",   placeholderColor = new Color(0.60f, 0.40f, 0.90f) },
    };

    [Header("Intensity → scale")]
    public float minScale = 0.05f;
    public float maxScale = 0.30f;

    [Header("Filtering / feel")]
    [Range(0f, 1f)] public float confidenceThreshold = 0.5f;  // ignore unsure frames
    public float smoothing = 6f;                              // higher = snappier
    public float idleTimeoutSeconds = 2f;                     // decay to nothing if no accepted packet for this long

    // Indexes in lockstep with odourVisuals, but entries with no prefab all
    // point at the SAME shared placeholder GameObject (see SpawnInstances) --
    // until real fruit models are assigned, "switching odour" means recolouring
    // that one mesh in place, not swapping between separate spheres.
    private GameObject[] _instances;
    private GameObject _sharedPlaceholder;
    private Renderer _sharedPlaceholderRenderer;
    private int _activeIndex = -1;
    private float _targetIntensity;
    private float _lastAcceptedRealtime = -999f;   // when Update last accepted a confident, matched packet

    private void Awake()
    {
        if (receiver == null) receiver = GetComponent<SmellReceiver>();
        SpawnInstances();
    }

    private void SpawnInstances()
    {
        _instances = new GameObject[odourVisuals.Length];
        for (int i = 0; i < odourVisuals.Length; i++)
        {
            if (odourVisuals[i].prefab != null)
            {
                GameObject go = Instantiate(odourVisuals[i].prefab, transform);
                go.name = $"Odour_{odourVisuals[i].odour}";
                go.transform.SetParent(transform, false);
                go.transform.localPosition = Vector3.zero;
                go.transform.localScale = Vector3.one * minScale;
                go.SetActive(false);
                _instances[i] = go;
            }
            else
            {
                _instances[i] = GetSharedPlaceholder();
            }
        }
    }

    private GameObject GetSharedPlaceholder()
    {
        if (_sharedPlaceholder != null) return _sharedPlaceholder;

        _sharedPlaceholder = GameObject.CreatePrimitive(PrimitiveType.Sphere);
        var collider = _sharedPlaceholder.GetComponent<Collider>();
        if (collider != null) Destroy(collider);   // visual only, no physics needed
        _sharedPlaceholder.name = "Odour_Placeholder";
        _sharedPlaceholder.transform.SetParent(transform, false);
        _sharedPlaceholder.transform.localPosition = Vector3.zero;
        _sharedPlaceholder.transform.localScale = Vector3.one * minScale;
        _sharedPlaceholder.SetActive(false);
        _sharedPlaceholderRenderer = _sharedPlaceholder.GetComponent<Renderer>();
        return _sharedPlaceholder;
    }

    private void Update()
    {
        if (receiver == null) return;

        if (receiver.HasReceivedData)
        {
            var p = receiver.LatestPacket;
            if (p.odour_confidence >= confidenceThreshold)
            {
                int idx = Array.FindIndex(odourVisuals, o => o.odour == p.odour);
                if (idx >= 0)
                {
                    SetActiveIndex(idx);
                    _targetIntensity = Mathf.Clamp01(p.intensity);
                    _lastAcceptedRealtime = Time.realtimeSinceStartup;
                }
            }
        }

        // Idle decay (plan.md §3 rule), keyed off the last ACCEPTED packet, not
        // the last received one: the server's strength gate keeps packets
        // flowing with odour_confidence forced to 0 while no odour is present,
        // so "packets arriving but none confident" must fade out too — keying
        // off receiver.LastPacketRealtime would freeze the visual at its last
        // confident scale forever. Also covers link-down (no packets at all)
        // and packets whose odour string matches no odourVisuals entry.
        if (Time.realtimeSinceStartup - _lastAcceptedRealtime > idleTimeoutSeconds)
            _targetIntensity = 0f;

        ApplyScale();
    }

    private void SetActiveIndex(int idx)
    {
        if (idx == _activeIndex) return;

        GameObject prevGo = _activeIndex >= 0 ? _instances[_activeIndex] : null;
        GameObject newGo = _instances[idx];

        // Switching between two placeholder-backed odours reuses the same
        // GameObject -- deactivating it here would just be undone by the
        // SetActive(true) below, and would needlessly interrupt the object
        // (losing its current scale to Unity's activation reset in edge
        // cases). Only touch prevGo when it's actually a different object.
        if (prevGo != null && prevGo != newGo)
            prevGo.SetActive(false);

        newGo.SetActive(true);
        if (newGo == _sharedPlaceholder)
            _sharedPlaceholderRenderer.material.color = odourVisuals[idx].placeholderColor;

        _activeIndex = idx;
    }

    private void ApplyScale()
    {
        if (_activeIndex < 0) return;

        // Smooth toward the target so per-frame inference noise doesn't jitter
        // (plan.md §6).
        var go = _instances[_activeIndex];
        float current = Mathf.InverseLerp(minScale, maxScale, go.transform.localScale.x);
        float lerped = Mathf.Lerp(current, _targetIntensity, Time.deltaTime * smoothing);
        float s = Mathf.Lerp(minScale, maxScale, Mathf.Clamp01(lerped));
        go.transform.localScale = new Vector3(s, s, s);
    }
}
