using UnityEngine;
using UnityEngine.UI;
using TMPro;
using System;

public class NumberSelectorUI : MonoBehaviour
{
    [Header("UI面板")]
    public GameObject inputPanel;   // 输入面板
    public GameObject resultPanel;  // 结果面板

    [Header("输入组件")]
    public TMP_InputField inputField;

    [Header("目标物体")]
    public GameObject parentObject;  // 包含18个子物体的父物体

    // 事件：数字确认、UI关闭
    public event Action<int> OnNumberConfirmed;
    public event Action OnUIClose;   // 用于通知外部清除生成物体

    private GameObject[] children;
    private Action onClose;  // 关闭整个UI时执行的回调（恢复水果模型和关联物体）

    void Start()
    {
        if (inputPanel != null) inputPanel.SetActive(false);
        if (resultPanel != null) resultPanel.SetActive(false);

        if (parentObject != null)
        {
            children = new GameObject[parentObject.transform.childCount];
            for (int i = 0; i < parentObject.transform.childCount; i++)
                children[i] = parentObject.transform.GetChild(i).gameObject;
            foreach (var child in children)
                child.SetActive(false);
        }
        else
        {
            Debug.LogError("parentObject未设置！");
        }
    }

    public void Show(Action onCloseCallback)
    {
        onClose = onCloseCallback;
        ShowInputPanel();
    }

    public void ShowInputPanel()
    {
        if (inputPanel != null) inputPanel.SetActive(true);
        if (resultPanel != null) resultPanel.SetActive(false);
        if (inputField != null)
        {
            inputField.Select();
            inputField.ActivateInputField();
        }
    }

    public void ShowResultPanel()
    {
        if (inputPanel != null) inputPanel.SetActive(false);
        if (resultPanel != null) resultPanel.SetActive(true);
    }

    public void Close()
    {
        if (inputPanel != null) inputPanel.SetActive(false);
        if (resultPanel != null) resultPanel.SetActive(false);
        onClose?.Invoke();          // 恢复隐藏的模型和关联物体
        OnUIClose?.Invoke();        // 清除生成物体
        if (inputField != null) inputField.text = "";
    }

    public void OnConfirm()
    {
        if (inputField == null || children == null || children.Length != 18)
        {
            Debug.LogWarning("输入框或子物体未正确初始化");
            return;
        }

        string input = inputField.text;
        if (int.TryParse(input, out int number) && number >= 1 && number <= 18)
        {
            int index = number - 1;
            for (int i = 0; i < children.Length; i++)
                children[i].SetActive(i == index);
            Debug.Log($"显示子物体 {number} 成功");

            OnNumberConfirmed?.Invoke(number);
            ShowResultPanel();
        }
        else
        {
            Debug.LogWarning("输入无效，请输入1-18的数字");
        }
    }

    public void OnBackToInput()
    {
        OnUIClose?.Invoke();  // 新增：清除生成物体
        ShowInputPanel();
    }

    public void OnBack()
    {
        Close();
    }
}