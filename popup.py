import tkinter as tk
import pyperclip
import subprocess
import threading
import time


def get_selected_text():
    '''
    retorna el texto seleccionado en el portapapeles
    '''
    try:
        selected_text = subprocess.check_output(
            ['xclip', '-o', '-selection', 'primary']).decode('utf-8')
        return selected_text
    except subprocess.CalledProcessError:
        return ''


class PopupWindow(tk.Toplevel):
    '''
    Clase del Popup
    '''

    def __init__(self, selected_text):
        super().__init__()
        self.geometry('1000x1000')
        self.title('Opciones')

        label = tk.Label(self, text='Texto seleccionado: ' + selected_text)
        label.pack()

        button_copy = tk.Button(
            self, text='Copiar',
            command=lambda: self.copy_to_clipboard(selected_text))
        button_copy.pack()

        self.protocol('WM_DELETE_WINDOW', self.close_popup)
        self.bind('<FocusOut>', self.close_popup)

    def close_popup(self, event=None):
        self.destroy()

    def copy_to_clipboard(self, text):
        pyperclip.copy(text)
        self.destroy()


def check_clipboard():
    '''
    Funcion que se ejecuta en un hilo para revisar el portapapeles
    '''
    previous_text = get_selected_text()
    current_popup = None

    while True:
        selected_text = get_selected_text()

        if selected_text != previous_text or not selected_text:
            if current_popup:
                current_popup.close_popup()
                current_popup = None

            if selected_text.strip() != '':
                current_popup = PopupWindow(selected_text)

            previous_text = selected_text
        time.sleep(2)


if __name__ == '__main__':
    root = tk.Tk()
    root.withdraw()

    clipboard_thread = threading.Thread(target=check_clipboard)
    clipboard_thread.daemon = True
    clipboard_thread.start()

    root.mainloop()
