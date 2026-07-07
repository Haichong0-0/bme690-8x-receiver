using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class CloseMain : MonoBehaviour
{
    // Start is called before the first frame update

    public GameObject MainUI;
    public GameObject Tip;

    void Start()
    {
        
    }

    // Update is called once per frame
    void Update()
    {
        
    }


    public void CloseUI() {



        MainUI.SetActive(false);

        Tip.SetActive(true);

    }


}
