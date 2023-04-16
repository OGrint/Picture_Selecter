import os
from screeninfo import get_monitors
from PIL import Image
import pygame
import sys

from buttons_and_more import Button
from menus import screen_1 
from menus import screen_2
from menus import screen_3

# extract monitor information
for m in get_monitors():
    screen_dim=str(m)

# calculate window width and height
width=int(screen_dim[screen_dim.find('width=')+len('width='):screen_dim.rfind(', height=')])-10
height=int(screen_dim[screen_dim.find(', height=')+len(', height='):screen_dim.rfind(', width_mm')])-60

# find centre of the screen
centre_h=height/2
centre_w=width/2

# set background color
background='White'

# initiate pygame
pygame.init()  
pygame.font.init()

# render window
screen = pygame.display.set_mode([width, height])
pygame.display.set_caption("Photo Selecter")
screen.fill(pygame.Color(background))

# empty string for selected file
f = "<No File Selected>"


# define which screen is active
screen_no=1

# run the game, switching between active menus
while True:
    if screen_no==1:
        screen_no,f=screen_1(screen,f,background, centre_w,centre_h)
    if screen_no==2:
        # extract outputs from 2 - including summary lists of files and locations
        jpeg_details,nef_details,jpeg_not_nef,screen_no=screen_2(screen,f,background,centre_w,centre_h)
    if screen_no==3:
        screen_no=screen_3(screen,f,jpeg_details,nef_details,jpeg_not_nef,background,centre_w,centre_h)
    



          
    
   
