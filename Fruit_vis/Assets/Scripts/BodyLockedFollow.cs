using UnityEngine;

/// <summary>
/// Body-locked "tag-along" + billboard follow, for HUD/auxiliary overlays only —
/// NOT for primary content, which should stay world-locked (plan.md's open
/// anchoring question: main visuals = world-locked, this is for the debug HUD).
///
/// Per Microsoft's Mixed Reality comfort guidance, rigid head-locked content
/// (1:1 translate + rotate with the camera) causes discomfort. The documented
/// fix — and what MRTK's RadialView/Follow solver implement — is: reposition
/// only once the target drifts outside a view cone (lazy-follow, lerped, never
/// snapped), and billboard-rotate smoothly to keep facing the user.
/// See: https://learn.microsoft.com/windows/mixed-reality/design/comfort
///      https://learn.microsoft.com/windows/mixed-reality/design/billboarding-and-tag-along
/// </summary>
public class BodyLockedFollow : MonoBehaviour
{
    [Tooltip("Camera to follow. Defaults to Camera.main.")]
    public Transform target;

    [Header("Tag-along (position)")]
    [Tooltip("Distance in front of the camera to place content, in metres. " +
             "HoloLens' optical focal plane is ~2.0 m — comfort guidance recommends " +
             "staying within 1.25-5 m.")]
    public float distance = 1.5f;

    [Tooltip("Offset from dead-centre, in metres, along the camera's right (x) and " +
             "up (y) axes. E.g. (-0.35, 0.18) parks the panel toward the top-left " +
             "of view instead of dead-centre.")]
    public Vector2 viewOffset = Vector2.zero;

    [Tooltip("Half-angle (degrees) of the view cone the panel is allowed to drift " +
             "within before it repositions (MRTK RadialView's min/max view degrees). " +
             "Must be comfortably larger than the angle viewOffset itself subtends, " +
             "or the panel never settles and rigidly tracks every frame instead of " +
             "tagging along.")]
    public float maxViewDegrees = 30f;

    [Tooltip("How quickly the panel slides to its new anchor once outside the view cone.")]
    public float moveLerpSpeed = 4f;

    [Header("Billboard (rotation)")]
    [Tooltip("How quickly the panel re-orients to face the user. Lerped, never snapped, " +
             "so it doesn't add sudden 1:1 rotation.")]
    public float rotateLerpSpeed = 6f;

    private Vector3 _anchorPosition;
    private bool _hasAnchor;

    private void Awake()
    {
        if (target == null && Camera.main != null) target = Camera.main.transform;
    }

    private void LateUpdate()
    {
        if (target == null) return;

        Vector3 toSelf = transform.position - target.position;
        float angleFromForward = _hasAnchor ? Vector3.Angle(target.forward, toSelf) : 999f;

        if (!_hasAnchor || angleFromForward > maxViewDegrees)
        {
            _anchorPosition = target.position + target.forward * distance
                              + target.right * viewOffset.x
                              + target.up * viewOffset.y;
            _hasAnchor = true;
        }

        transform.position = Vector3.Lerp(transform.position, _anchorPosition,
                                           Time.deltaTime * moveLerpSpeed);

        Quaternion desiredRotation = Quaternion.LookRotation(
            transform.position - target.position, Vector3.up);
        transform.rotation = Quaternion.Slerp(transform.rotation, desiredRotation,
                                               Time.deltaTime * rotateLerpSpeed);
    }
}
