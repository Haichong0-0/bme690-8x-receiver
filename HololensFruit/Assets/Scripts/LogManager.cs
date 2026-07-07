using UnityEngine;
using System.Net;
using System.Net.Sockets;
using System.Text;

public class LogManager : MonoBehaviour
{
    public static LogManager Instance { get; private set; }

    [Header("PC端接收配置")]
    public string targetIP = "192.168.1.100"; // 替换为PC的IP地址
    public int targetPort = 8888;              // 接收端口

    private UdpClient udpClient;
    private IPEndPoint remoteEndPoint;

    void Awake()
    {
        if (Instance != null && Instance != this)
        {
            Destroy(gameObject);
            return;
        }
        Instance = this;
        DontDestroyOnLoad(gameObject);

        try
        {
            udpClient = new UdpClient();
            remoteEndPoint = new IPEndPoint(IPAddress.Parse(targetIP), targetPort);
        }
        catch (System.Exception e)
        {
            Debug.LogError("UDP初始化失败：" + e.Message);
        }
    }

    /// <summary>
    /// 记录操作日志并发送到PC端
    /// </summary>
    /// <param name="category">操作类型：模型显隐 / 模型交互</param>
    /// <param name="name">模型名称 或 具体动作（放大/缩小/复位）</param>
    /// <param name="delay">延迟时间（秒）</param>
    public void Log(string category, string name, float delay)
    {
        string logEntry = string.Format("{0},{1},{2:F6}", category, name, delay);
        Debug.Log("日志：" + logEntry);

        if (udpClient != null)
        {
            byte[] data = Encoding.UTF8.GetBytes(logEntry + "\n");
            udpClient.Send(data, data.Length, remoteEndPoint);
        }
        else
        {
            Debug.LogWarning("UDP客户端未初始化，日志未发送");
        }
    }

    void OnDestroy()
    {
        if (udpClient != null)
            udpClient.Close();
    }
}