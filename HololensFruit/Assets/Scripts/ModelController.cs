using UnityEngine;
using UnityEngine.Windows.Speech;
using System.Collections.Generic;
using System.Diagnostics;
using System;
using Debug = UnityEngine.Debug;

public class ModelController : MonoBehaviour
{
    [Header("模型列表（10个）")]
    public GameObject[] models = new GameObject[10];

    [Header("生成用的预制体列表（10个，对应每个模型）")]
    public GameObject[] spawnPrefabs = new GameObject[10];

    [Header("每个模型关联的额外物体（用于隐藏/恢复）")]
    public GameObject[] associatedObjects = new GameObject[10];

    [Header("每个模型对应的三个辅助物体（按钮控制显隐）")]
    public GameObject[][] auxObjects;

    [Header("数字选择UI引用")]
    public NumberSelectorUI numberSelectorUI;

    [Header("生成物体配置")]
    public float spawnRadius = 0.5f;           // 半径改为1米
    public Camera targetCamera;               // 参考相机

    private Vector3[] initialPositions;
    private Quaternion[] initialRotations;
    private Vector3[] initialScales;
    private int currentModelIndex = -1;
    private GameObject currentModel;

    private KeywordRecognizer keywordRecognizer;
    private Dictionary<string, Action> keywordActions;

    private Stopwatch currentStopwatch;
    private bool isRecordingDelay = false;

    // 存储当前生成的物体，用于销毁
    private List<GameObject> spawnedObjects = new List<GameObject>();

    void Awake()
    {
        auxObjects = new GameObject[10][];
        for (int i = 0; i < 10; i++)
            auxObjects[i] = new GameObject[3];
    }

    void Start()
    {
        // 记录初始状态
        initialPositions = new Vector3[models.Length];
        initialRotations = new Quaternion[models.Length];
        initialScales = new Vector3[models.Length];
        for (int i = 0; i < models.Length; i++)
        {
            if (models[i] != null)
            {
                initialPositions[i] = models[i].transform.position;
                initialRotations[i] = models[i].transform.rotation;
                initialScales[i] = models[i].transform.localScale;
                models[i].SetActive(false);
            }
            else
            {
                Debug.LogError("模型索引 " + i + " 未分配！");
            }
        }

        // 语音识别
        keywordActions = new Dictionary<string, Action>();
        string[] modelKeywords = new string[]
        {
            "Apple", "Banana", "WaterMelon", "Pineapple", "Orange",
            "Pear", "Kiwi", "Lemon", "Plum", "Strawberry"
        };
        for (int i = 0; i < modelKeywords.Length; i++)
        {
            int index = i;
            keywordActions[modelKeywords[i]] = () => ShowModelByIndex(index);
        }
        keywordActions["Magnify"] = () => ScaleModel(1.2f);
        keywordActions["Reduce"] = () => ScaleModel(0.8f);
        keywordActions["Reset"] = () => ResetModel();

        keywordRecognizer = new KeywordRecognizer(new List<string>(keywordActions.Keys).ToArray());
        keywordRecognizer.OnPhraseRecognized += OnPhraseRecognized;
        keywordRecognizer.Start();

        Debug.Log("语音识别已启动，可用指令：" + string.Join(", ", keywordActions.Keys));

        // 注册数字确认事件和UI关闭事件
        if (numberSelectorUI != null)
        {
            numberSelectorUI.OnNumberConfirmed += OnNumberConfirmed;
            numberSelectorUI.OnUIClose += OnUIClose;   // 新增事件：UI关闭时清除生成物
        }
        else
        {
            Debug.LogError("NumberSelectorUI未引用！");
        }

        // 获取相机
        if (targetCamera == null)
            targetCamera = Camera.main;
        if (targetCamera == null)
            Debug.LogWarning("未设置参考相机，生成物体将使用世界原点！");
    }

    private void OnPhraseRecognized(PhraseRecognizedEventArgs args)
    {
        currentStopwatch = Stopwatch.StartNew();
        isRecordingDelay = true;
        if (keywordActions.ContainsKey(args.text))
            keywordActions[args.text].Invoke();
        isRecordingDelay = false;
    }

    private void ShowModelByIndex(int index)
    {
        if (index < 0 || index >= models.Length || models[index] == null) return;

        if (currentModel != null)
            currentModel.SetActive(false);

        currentModel = models[index];
        currentModelIndex = index;
        currentModel.transform.position = initialPositions[index];
        currentModel.transform.rotation = initialRotations[index];
        currentModel.transform.localScale = initialScales[index];
        currentModel.SetActive(true);

        if (isRecordingDelay && currentStopwatch != null)
        {
            float delay = (float)currentStopwatch.Elapsed.TotalSeconds;
            LogManager.Instance.Log("ModelVisibility", currentModel.name, delay);
        }
        else
        {
            Debug.LogWarning("延迟记录失败：计时器未启动");
        }
    }

    private void ScaleModel(float factor)
    {
        if (currentModel == null)
        {
            Debug.LogWarning("没有激活的模型，无法缩放");
            return;
        }

        currentModel.transform.localScale *= factor;

        if (isRecordingDelay && currentStopwatch != null)
        {
            float delay = (float)currentStopwatch.Elapsed.TotalSeconds;
            string actionName = factor > 1 ? "Magnify" : "Reduce";
            LogManager.Instance.Log("ModelInteraction", actionName, delay);
        }
        else
        {
            Debug.LogWarning("延迟记录失败：计时器未启动");
        }
    }

    private void ResetModel()
    {
        if (currentModel == null || currentModelIndex < 0)
        {
            Debug.LogWarning("没有激活的模型，无法复位");
            return;
        }

        currentModel.transform.position = initialPositions[currentModelIndex];
        currentModel.transform.rotation = initialRotations[currentModelIndex];
        currentModel.transform.localScale = initialScales[currentModelIndex];

        if (isRecordingDelay && currentStopwatch != null)
        {
            float delay = (float)currentStopwatch.Elapsed.TotalSeconds;
            LogManager.Instance.Log("ModelInteraction", "Reset", delay);
        }
        else
        {
            Debug.LogWarning("延迟记录失败：计时器未启动");
        }
    }

    // 切换辅助物体
    public void ToggleAuxObject(int modelIndex, int buttonIndex)
    {
        if (modelIndex < 0 || modelIndex >= models.Length || buttonIndex < 0 || buttonIndex >= 3)
        {
            Debug.LogError("参数超出范围");
            return;
        }

        GameObject obj = auxObjects[modelIndex][buttonIndex];
        if (obj != null)
            obj.SetActive(!obj.activeSelf);
        else
            Debug.LogWarning($"模型 {modelIndex} 按钮 {buttonIndex} 未关联辅助物体");
    }

    // 数字选择按钮调用的方法
    public void OnNumberSelectButton(int modelIndex)
    {
        if (modelIndex < 0 || modelIndex >= models.Length) return;

        int selectedModelIndex = modelIndex;

        // 隐藏模型和关联物体
        GameObject modelToHide = models[modelIndex];
        GameObject objToHide = associatedObjects[modelIndex];

        if (modelToHide != null && modelToHide.activeSelf)
            modelToHide.SetActive(false);
        if (objToHide != null && objToHide.activeSelf)
            objToHide.SetActive(false);

        // 显示UI，并传入恢复回调
        if (numberSelectorUI != null)
        {
            numberSelectorUI.Show(() => RestoreObjects(selectedModelIndex));
        }
        else
        {
            Debug.LogError("NumberSelectorUI未引用！");
        }
    }

    // 恢复被隐藏的模型和关联物体
    private void RestoreObjects(int modelIndex)
    {
        if (modelIndex < 0 || modelIndex >= models.Length) return;

        GameObject modelToRestore = models[modelIndex];
        GameObject objToRestore = associatedObjects[modelIndex];

        if (modelToRestore != null && !modelToRestore.activeSelf)
            modelToRestore.SetActive(true);
        if (objToRestore != null && !objToRestore.activeSelf)
            objToRestore.SetActive(true);
    }

    // 数字确认事件处理：生成两个对应的预制体物体
    private void OnNumberConfirmed(int number)
    {
        // 清除之前生成的物体（在生成新物体前清理，避免堆积）
        ClearSpawnedObjects();

        // 检查当前是否有激活的模型
        if (currentModelIndex < 0 || currentModelIndex >= spawnPrefabs.Length)
        {
            Debug.LogWarning("当前没有激活的模型，无法生成物体");
            return;
        }

        // 获取对应的预制体
        GameObject prefab = spawnPrefabs[currentModelIndex];
        if (prefab == null)
        {
            Debug.LogWarning($"模型索引 {currentModelIndex} 未设置生成预制体，无法生成物体");
            return;
        }

        // 计算两个角度：以相机正后方（180°）为基准，左右对称偏移
        float offset = (number - 1) * 10;
        float angle1 = 180 + offset;  // 右后方
        float angle2 = 180 - offset;  // 左后方

        // 生成两个物体
        SpawnFruitAtAngle(angle1, prefab);
        SpawnFruitAtAngle(angle2, prefab);
    }

    // 在指定角度生成预制体物体
    private void SpawnFruitAtAngle(float angleDeg, GameObject prefab)
    {
        if (prefab == null) return;

        Camera cam = targetCamera != null ? targetCamera : Camera.main;
        if (cam == null)
        {
            Debug.LogWarning("无可用相机，生成物体失败");
            return;
        }

        // 计算生成位置
        Quaternion rotation = Quaternion.Euler(0, angleDeg, 0);
        Vector3 direction = rotation * Vector3.forward;
        Vector3 worldDir = cam.transform.TransformDirection(direction);
        Vector3 spawnPos = cam.transform.position + worldDir * spawnRadius;

        // 实例化预制体
        GameObject newObj = Instantiate(prefab, spawnPos, Quaternion.identity);
        newObj.SetActive(true);

        // 使物体正面朝向相机
        Vector3 dirToCamera = cam.transform.position - spawnPos;
        if (dirToCamera != Vector3.zero)
            newObj.transform.rotation = Quaternion.LookRotation(dirToCamera);

        spawnedObjects.Add(newObj);
        Debug.Log($"生成物体：{prefab.name} 在角度 {angleDeg}°，位置 {spawnPos}");
    }

    // 清除所有生成的物体
    private void ClearSpawnedObjects()
    {
        foreach (var obj in spawnedObjects)
            if (obj != null) Destroy(obj);
        spawnedObjects.Clear();
    }

    // UI关闭时的回调（清除生成物体）
    private void OnUIClose()
    {
        ClearSpawnedObjects();
    }

    // 辅助设置方法
    public void SetAuxObject(int modelIndex, int buttonIndex, GameObject obj)
    {
        if (auxObjects == null) auxObjects = new GameObject[10][];
        if (auxObjects[modelIndex] == null) auxObjects[modelIndex] = new GameObject[3];
        auxObjects[modelIndex][buttonIndex] = obj;
    }

    void OnDestroy()
    {
        if (keywordRecognizer != null && keywordRecognizer.IsRunning)
            keywordRecognizer.Stop();

        ClearSpawnedObjects();
    }
}