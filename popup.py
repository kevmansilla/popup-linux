import tkinter as tk
import pyperclip
import subprocess
import threading
import time
import webbrowser


def get_selected_text():
    try:
        selected_text = subprocess.check_output(
            ['xclip', '-o', '-selection', 'primary']).decode('utf-8')
        return selected_text
    except subprocess.CalledProcessError:
        return ''


class PopupWindow(tk.Toplevel):
    def __init__(self, selected_text):
        super().__init__()
        self.geometry('900x600')
        self.title('Opciones')

        # Etiqueta descriptiva
        label = tk.Label(self, text='Texto seleccionado:')
        label.pack()

        # Cuadro de texto para mostrar el texto seleccionado
        text_box = tk.Text(self, height=10, width=90)
        text_box.insert(tk.END, selected_text)
        text_box.pack()

        # Botón de copiar
        button_copy = tk.Button(
            self, text='Copiar',
            command=lambda: self.copy_to_clipboard(selected_text))
        button_copy.pack()

        # boton para buscar en google
        button_search_google = tk.Button(
            self, text='Buscar en Google',
            command=lambda: webbrowser.open(
                'https://www.google.com/search?q=' + selected_text))
        button_search_google.pack()

        # boton para traducir
        button_translate = tk.Button(
            self, text='Traducir',
            command=lambda: webbrowser.open(
                'https://translate.google.com/?sl=auto&tl=es&text=' + selected_text))
        button_translate.pack()

        self.protocol('WM_DELETE_WINDOW', self.close_popup)
        self.bind('<FocusOut>', self.close_popup)
        self.bind('<Escape>', self.close_popup)

    def close_popup(self, event=None):
        self.destroy()

    def copy_to_clipboard(self, text):
        pyperclip.copy(text)
        self.destroy()


def check_clipboard():
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
