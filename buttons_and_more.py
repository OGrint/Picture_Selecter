import pygame
import tkinter
import tkinter.filedialog

class Button():
    # class for rectangle that can have text in 
    def __init__(self,font_size,font_color,text,centre_width,centre_height,width,height,color,screen):
        # intitalise button with all required parameters
        self.font = pygame.font.Font(None, font_size)
        self.font_color=font_color
        self.text=text
        # calculate width edges
        width_edge=centre_width-(width/2)
        height_edge=centre_height-(height/2)
        # calculate the rectangle area
        self.area=pygame.Rect(width_edge, height_edge, width, height)
        self.color = pygame.Color(color)
        self.screen=screen


    def draw_box(self,x_space,y_space):
        # draw the rectangle
        pygame.draw.rect(self.screen, self.color, self.area)
  
        # write the text on the surface
        text_surface = self.font.render(self.text, True, (self.font_color))
    
        # render at position stated in arguments
        self.screen.blit(text_surface, (self.area.x+x_space, self.area.y+y_space))

def prompt_file(background_color):
    # Open a file explorer to select the directory
    top = tkinter.Tk()
    top.withdraw()  # hide window
    file_name = tkinter.filedialog.askdirectory(parent=top)
    top.destroy()
    return file_name

def wipe_screen(background_color,screen):
    # clear the screen
    screen.fill(background_color)
    pygame.display.update()
