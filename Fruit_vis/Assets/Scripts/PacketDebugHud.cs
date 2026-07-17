using TMPro;
using UnityEngine;
using UnityEngine.UI;

/// <summary>
/// Dev-time overlay showing the raw §3 packet exactly as it arrives on the wire —
/// unfiltered by confidence threshold/smoothing, so you can see what the
/// visualiser is choosing to ignore as well as what it's showing.
///
/// BODY-LOCKED, not head-locked: Microsoft's Mixed Reality comfort guidance
/// explicitly warns that HUDs rigidly parented to the camera (1:1 translate +
/// rotate) cause discomfort, and recommends body-locking instead (see
/// BodyLockedFollow.cs). This script builds its own Canvas + Text at runtime
/// and adds that component instead of parenting to the camera; just drop this
/// on any object (Main Camera is the usual choice) — no scene setup or prefab
/// needed. Remove/disable this component for a release build.
/// </summary>
public class PacketDebugHud : MonoBehaviour
{
    [Tooltip("Receiver to read the latest packet from. Auto-found if left empty.")]
    public SmellReceiver receiver;

    [Header("Placement (body-locked tag-along, see BodyLockedFollow)")]
    [Tooltip("Metres in front of the camera. Comfort guidance: 1.25-5 m zone, " +
             "~2.0 m matches HoloLens' optical focal plane.")]
    public float distance = 1.4f;
    [Tooltip("Offset from dead-centre (camera right/up axes), in metres. " +
             "Default parks the panel toward the top-left of view.")]
    public Vector2 viewOffset = new Vector2(-0.35f, 0.18f);
    public Vector2 panelSizeMetres = new Vector2(0.5f, 0.3f);

    private TMP_Text _text;

    private void Awake()
    {
        if (receiver == null) receiver = FindAnyObjectByType<SmellReceiver>();
        BuildUI();
    }

    private void BuildUI()
    {
        var canvasGO = new GameObject("SmellDebugHud_Canvas");
        var follow = canvasGO.AddComponent<BodyLockedFollow>();
        follow.distance = distance;
        follow.viewOffset = viewOffset;

        var canvas = canvasGO.AddComponent<Canvas>();
        canvas.renderMode = RenderMode.WorldSpace;
        canvasGO.AddComponent<CanvasScaler>();
        canvasGO.AddComponent<GraphicRaycaster>();

        const float pixelsWide = 500f, pixelsTall = 300f;
        var rt = canvasGO.GetComponent<RectTransform>();
        rt.sizeDelta = new Vector2(pixelsWide, pixelsTall);
        canvasGO.transform.localScale = new Vector3(
            panelSizeMetres.x / pixelsWide, panelSizeMetres.y / pixelsTall, 1f);

        var panelGO = new GameObject("Panel");
        panelGO.transform.SetParent(canvasGO.transform, false);
        var panelImg = panelGO.AddComponent<Image>();
        panelImg.color = new Color(0f, 0f, 0f, 0.65f);
        var panelRt = panelGO.GetComponent<RectTransform>();
        panelRt.anchorMin = Vector2.zero;
        panelRt.anchorMax = Vector2.one;
        panelRt.offsetMin = Vector2.zero;
        panelRt.offsetMax = Vector2.zero;

        var textGO = new GameObject("Text");
        textGO.transform.SetParent(panelGO.transform, false);
        // TextMeshPro renders from a signed-distance-field atlas, so text stays crisp
        // at any magnification (HoloLens optics or a zoomed Game view) with no
        // dynamicPixelsPerUnit trick. It uses TMP's default font asset, which needs
        // TMP Essentials imported (Window > TextMeshPro > Import TMP Essential
        // Resources) — without them the default font is null and text renders blank,
        // so warn loudly rather than fail silently.
        var tmp = textGO.AddComponent<TextMeshProUGUI>();
        tmp.fontSize = 22;
        tmp.alignment = TextAlignmentOptions.TopLeft;
        tmp.color = Color.white;
        tmp.textWrappingMode = TextWrappingModes.NoWrap;   // keep each aligned column line intact
        tmp.richText = false;             // packet text is literal — no markup
        _text = tmp;
        if (tmp.font == null)
            Debug.LogError("[PacketDebugHud] TextMeshPro has no font asset — run " +
                "Window > TextMeshPro > Import TMP Essential Resources, then re-enter Play.");
        var textRt = textGO.GetComponent<RectTransform>();
        textRt.anchorMin = Vector2.zero;
        textRt.anchorMax = Vector2.one;
        textRt.offsetMin = new Vector2(14, 14);
        textRt.offsetMax = new Vector2(-14, -14);
    }

    private void Update()
    {
        if (_text == null) return;

        if (receiver == null)
        {
            _text.text = "SmellReceiver not found in scene.";
            return;
        }

        if (!receiver.HasReceivedData)
        {
            _text.text =
                $"url: {receiver.serverUrl}\n" +
                (receiver.IsConnected
                    ? "link: CONNECTED\nwaiting for first packet..."
                    : "link: DISCONNECTED\nreconnecting...");
            return;
        }

        var p = receiver.LatestPacket;
        float age = Time.realtimeSinceStartup - receiver.LastPacketRealtime;

        _text.text =
            $"url:         {receiver.serverUrl}\n" +
            $"link:        {(receiver.IsConnected ? "CONNECTED" : "DISCONNECTED")}\n" +
            $"rate:        {receiver.PacketsPerSecond} Hz\n" +
            $"last packet: {age:F2}s ago\n" +
            $"\n" +
            $"seq:         {p.seq}\n" +
            $"timestamp:   {p.timestamp:F3}\n" +
            $"odour:       {p.odour}\n" +
            $"confidence:  {p.odour_confidence:F3}\n" +
            $"intensity:   {p.intensity:F3}";
    }
}
