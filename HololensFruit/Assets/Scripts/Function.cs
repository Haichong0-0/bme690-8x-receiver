using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class Function : MonoBehaviour
{


    public GameObject UI_1;
    public GameObject UI_2;
    public GameObject UI_3;


    // Start is called before the first frame update
    void Start()
    {
        
    }

    // Update is called once per frame
    void Update()
    {
        
    }

    public void DisplayUI1() {

        UI_2.SetActive(false);

        UI_3.SetActive(false);

        if (UI_1.activeInHierarchy == true)
        {

            UI_1.SetActive(false);
        }
        else {

            UI_1.SetActive(true);
        }
    
    }

    public void DisplayUI2() {
        UI_1.SetActive(false);

        UI_3.SetActive(false);

        if (UI_2.activeInHierarchy == true)
        {

            UI_2.SetActive(false);
        }
        else
        {

            UI_2.SetActive(true);
        }

    }


    public void DisplayUI3()
    {
        UI_1.SetActive(false);

        UI_2.SetActive(false);

        if (UI_3.activeInHierarchy == true)
        {

            UI_3.SetActive(false);
        }
        else
        {

            UI_3.SetActive(true);
        }

    }
}
