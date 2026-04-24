- scripts like same as have different versions regarding if it is BL or BSP, should be merged
- create README files for each folder explaining the scripts inside and how to use
- rename scripts

- TPUpdater:
    - Check if "Same as" TC links (i.e.: links foreign to the function of the TP work item) are correct, and add/remove as needed.
    - When a branch is already merged into wassp-jenkins the script shouldn't check for internal reference ccr links (how can it tell which ccr to link to? maybe should just ignore them at this point). If it is executed after merge it will mess up the internal reference links. (Could tis be done by checking merge requests associated with branch maybe??)
