# MangaDownload
Download manga's with this simple python script. Enter the link and wait till it downloads and then you have your chapters.

# Main
The main script is the main2.py that is the script that downloads the manga pages.  
It uses chromium for linux and tries to use chrome for windows but you will have to add your own dir for windows so the loaction of your chrome.  
```bash
pip install DrissionPage requests
```
These are all the pip installs you need for the main2.py.  
The select.py is the other script that is used to save the not saved chapters that couldn't be saved.  
Also now you have to manualy make the cover folder with the cover.jpg and info.txt inside for it to render the cover in the app and the info is just the info.  
I'll Probably upload or atleast try to upload another file main3.py where it handels the cover and the info by it self.  

# Flask
After the downloaded manga's you need to put it in the mangas folder like shown in the app folder.  
I have striped the source code from my own page and also the pages. You need to change the password inside the app.py because now it is just an example like Your_username, Your_password.  
My source code also handeled anime's so if the code looks a bit chunky or bare it is probably because of that the same for all the html files.  
How to run it? You can use flask but that is not recomened.
```bash
cd app
# For testing just use: "python app.py"
# For real streaming like so that you can also reach it on your phone i recommend gunicorn but use what you want
pip install gunicorn
pip install gevent
gunicorn -k gevent -w 4 app:app # You can use how many workers you want
```

  
This was my first upload after a few months. Hope you guys enjoy it 😄
