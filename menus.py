import os
import pygame
import tkinter
import tkinter.filedialog
import time
import sys
import shutil

from buttons_and_more import Button
from file_summary import image_stats
from NEF_JPEG_converter import convert_nef_to_jpeg
from picture_viewer import ImageOpener
from buttons_and_more import prompt_file
from buttons_and_more import wipe_screen


# the opening screen - this will have a next button, a title and an option to pick the directory
def screen_1(screen,f,background,centre_w,centre_h):
    # load a blank screen to clear any previous shapes
    screen.fill('White')

    # work out if the current input path is valid
    if os.path.exists(f):
        valid_file=True # log valid fle
        next_button_color='Light Blue' # next button can be clicked
    else:
        valid_file=False # log not valid file
        next_button_color='Dark Grey' # grey out next button


    # load in a button to select directory with
    button_1=Button(32,background,'Click to select directory',centre_w,centre_h+80,300,40,'Navy Blue',screen)
    button_1.draw_box(28,9)

    # load in the header
    header=Button(200,'Black','Photo Selecter',centre_w,centre_h-250,1000,200,background,screen)
    header.draw_box(28,9)

    # load in the folder header box
    folder_header=Button(32,'Black','Folder',centre_w-500,centre_h,120,40,'Dark Grey',screen)
    folder_header.draw_box(28,9)

    # load in the directory display box
    file_header=Button(32,'Black',f,centre_w+60,centre_h,1000,40,'Light Grey',screen)
    file_header.draw_box(28,9)
        
    # load in the next button
    next_button_1=Button(70,'Black','Next',centre_w+450,centre_h+300,220,70,next_button_color,screen)
    next_button_1.draw_box(55,12)

    # run the page
    while True:
        for event in pygame.event.get():
            # if user types QUIT then the screen will close
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            if event.type == pygame.MOUSEBUTTONDOWN:
                # if button 1 clicked open prompt to select directory
                if button_1.area.collidepoint(event.pos):
                    f = prompt_file(background)

                    # reload header with selected file
                    file_header=Button(32,'Black',f,centre_w+60,centre_h,1000,40,'Light Grey',screen)
                    file_header.draw_box(28,9)
                    
                    # check if new location is valid
                    if os.path.exists(f):
                         # if it is valid log this and make next button blue for clickable
                        next_button_1=Button(70,'Black','Next',centre_w+450,centre_h+300,220,70,'Light Blue',screen)
                        next_button_1.draw_box(55,12)
                        valid_file=True

                    else:
                         # if it is not valid log this and make next button greyed out
                        next_button_1=Button(70,'Black','Next',centre_w+450,centre_h+300,220,70,'Light Grey',screen)
                        next_button_1.draw_box(55,12)
                        valid_file=False

                # if valid file selected and next button clicked move to next page
                if next_button_1.area.collidepoint(event.pos)and valid_file==True:
                    return(2,f)
                
        pygame.display.update()

# menu 2 will show the stats for types of images and have the option to convert nef to jpg
def screen_2(screen,f,background,centre_w,centre_h):
    
    # fill the screen blank
    screen.fill('White')

    # analyse the stats of the images in the directory
    nef_not_jpeg,nef_and_jpeg,jpeg_not_nef,jpeg_details,nef_details=image_stats(f)

    # load in the header
    header=Button(100,'Black','Image Pre-processing',centre_w,centre_h-375,1000,200,background,screen)
    header.draw_box(145,9)

    # load in the Table to display stats - header
    box1=Button(45,'Black','File Type',centre_w-175,centre_h-300,350,40,'Dark Grey',screen)
    box1.draw_box(100,6)
    # load header box 
    box2=Button(45,'Black','No. Files',centre_w+175,centre_h-300,350,40,'Dark Grey',screen)
    box2.draw_box(110,6)
    # load in the Table to display stats - 1st row
    box3=Button(45,'Black','NEF as JPEG',centre_w-175,centre_h-260,350,40,'Light Blue',screen)
    box3.draw_box(60,6)
    box4=Button(45,'Black',str(len(nef_and_jpeg)),centre_w+175,centre_h-260,350,40,'Light Blue',screen)
    box4.draw_box(160,6)
    # load in the Table to display stats - 2nd row
    box5=Button(45,'Black','NEF no JPEG',centre_w-175,centre_h-220,350,40,'Light Blue',screen)
    box5.draw_box(60,6)
    box6=Button(45,'Black',str(len(nef_not_jpeg)),centre_w+175,centre_h-220,350,40,'Light Blue',screen)
    box6.draw_box(160,6)
    # load in the Table to display stats - 3rd row
    box7=Button(45,'Black','Other JPEG',centre_w-175,centre_h-180,350,40,'Light Blue',screen)
    box7.draw_box(60,6)
    box8=Button(45,'Black',str(len(jpeg_not_nef)),centre_w+175,centre_h-180,350,40,'Light Blue',screen)
    box8.draw_box(160,6)
    # load dividing line
    box9=Button(1,'Black','',centre_w,centre_h-240,4,160,'Black',screen)
    box9.draw_box(15,5)
    # load dividing line
    box10=Button(1,'Black','',centre_w,centre_h-280,700,4,'Black',screen)
    box10.draw_box(15,5)

    # if there are no jpeg images grey out the next button
    if len(nef_and_jpeg)>0 or len(jpeg_not_nef)>0 :
        next_color='Light Blue'
    else:
        next_color='Dark Grey'

     # load in the convert button
    convert_button=Button(45,'White','Convert to JPEG',centre_w,centre_h-85,300,50,'Dark Blue',screen)
    convert_button.draw_box(30,13)

    # load in the next button
    next_button_2=Button(70,'Black','Next',centre_w+450,centre_h+300,220,70,next_color,screen)
    next_button_2.draw_box(55,12)

     # load in the previous button
    prev_button_2=Button(70,'Black','Previous',centre_w-450,centre_h+300,220,70,'Light Blue',screen)
    prev_button_2.draw_box(5,12)

    # run the menu
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            if event.type == pygame.MOUSEBUTTONDOWN:
                # if the next button is clicked move to the next page (only if there are jpgs)
                if next_button_2.area.collidepoint(event.pos):
                    if len(nef_and_jpeg)>0 or len(jpeg_not_nef)>0 :
                        wipe_screen(background,screen)
                        return(jpeg_details,nef_details,jpeg_not_nef,3)

                # move to previous screen if previous button clicked
                if prev_button_2.area.collidepoint(event.pos):
                    wipe_screen(background,screen)
                    return(jpeg_details,nef_details,jpeg_not_nef,1)

                # convert button converts nefs without jpgs to jpgs
                if convert_button.area.collidepoint(event.pos):
                    
                    # load in a loading message
                    loading_button=Button(32,'Black','Loading... Please Wait',centre_w,centre_h,300,50,background,screen)
                    loading_button.draw_box(30,13)
                    loading_button=Button(32,'Black','',centre_w,centre_h+50,500,40,'Black',screen)
                    loading_button.draw_box(40,13)
                    loading_button=Button(32,'Black','',centre_w-120,centre_h+50,243,30,background,screen)
                    loading_button.draw_box(40,13)
                    loading_button=Button(32,'Black','Do not turn off app',centre_w,centre_h+100,300,50,background,screen)
                    loading_button.draw_box(40,13)
    
                    pygame.display.update()

                    # convert nefs to jpegs
                    convert_nef_to_jpeg(f,nef_not_jpeg)

                    # load in the new colored next button
                    next_button_2=Button(70,'Black','Next',centre_w+450,centre_h+300,220,70,'Light Blue',screen)
                    next_button_2.draw_box(55,12)

                    # blank out loading screen
                    loading_button=Button(32,'Black','',centre_w,centre_h+50,500,40,background,screen)
                    loading_button.draw_box(40,13)
                    loading_button=Button(32,'Black','',centre_w,centre_h,300,50,background,screen)
                    loading_button.draw_box(30,13)
                    loading_button=Button(32,'Black','',centre_w,centre_h+100,300,50,background,screen)
                    loading_button.draw_box(30,13)
                    
                    # load in a processing complete message
                    loading_button=Button(70,'Black','Processing Complete',centre_w,centre_h,470,50,background,screen)
                    loading_button.draw_box(0,13)
                    pygame.display.update()
                    time.sleep(2)
                    # blank out message
                    loading_button=Button(40,'Black','',centre_w,centre_h+50,500,40,background,screen)
                    loading_button.draw_box(5,13)

                    # re-enter page to reload stats
                    return(jpeg_details,nef_details,jpeg_not_nef,2)

        pygame.display.update()



# screen 3 will cycle through images, you can move them into a 'Best' folder or delete them from it
def screen_3(screen,f,jpeg_details,nef_details,jpeg_not_nef,background,centre_w,centre_h):
    
    # blank out the screen
    screen.fill('White')

    # load in the image name header box
    name_header=Button(32,'Black','Name',centre_w-150,centre_h-450,120,40,'Dark Grey',screen)
    name_header.draw_box(23,9)

    # load in the name display box
    name_box=Button(32,'Black','',centre_w+50,centre_h-450,300,40,'Light Grey',screen)
    name_box.draw_box(23,9)

     # load in the previous button
    prev_button_3=Button(70,'Black','Previous',centre_w-700,centre_h-450,220,70,'Light Blue',screen)
    prev_button_3.draw_box(5,12)

    # load in the finish button
    finish_button_3=Button(70,'Black','Finish',centre_w+700,centre_h-450,220,70,'Light Blue',screen)
    finish_button_3.draw_box(35,12)

    # load in the back button to move between images
    back_button=Button(50,'White','<',centre_w-213,centre_h-390,75,38,'Dark Blue',screen)
    back_button.draw_box(25,0)

    # load in the forward button to move between images
    forward_button=Button(50,'White','>',centre_w+213,centre_h-390,75,38,'Dark Blue',screen)
    forward_button.draw_box(25,0)

    # load in the pick button 
    pick_button=Button(35,'White','Pick',centre_w+113,centre_h-390,100,38,'Dark Blue',screen)
    pick_button.draw_box(25,7)

    # load in a delete button
    del_button=Button(35,'White','Delete',centre_w-113,centre_h-390,100,38,'Dark Blue',screen)
    del_button.draw_box(14,7)

    # load in an indicator for if the image is in the best folder
    best_button=Button(35,'Black','Best',centre_w,centre_h-390,100,38,'Light Grey',screen)
    best_button.draw_box(23,7)

    # load in the image opener
    ImageUI=ImageOpener(f,jpeg_details,nef_details,jpeg_not_nef,centre_w,centre_h,screen,background)

    # run the page
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            if event.type == pygame.MOUSEBUTTONDOWN:

                # move to previous screen
                if prev_button_3.area.collidepoint(event.pos):
                    wipe_screen(background,screen)
                    return(2)
                
                # finish app and delete temporary file
                if finish_button_3.area.collidepoint(event.pos):
                    shutil.rmtree(f+'/jpegs_temp')
                    pygame.quit()
                    sys.exit()
                
                # move to the image to the left
                if back_button.area.collidepoint(event.pos):
                    ImageUI.move_left()
                    pygame.display.update()
                
                # move to the image to the right
                if forward_button.area.collidepoint(event.pos):
                    ImageUI.move_right()
                    pygame.display.update()

                # move the current image to the best folder
                if pick_button.area.collidepoint(event.pos):
                    ImageUI.move_image()

                # remove the current image from the best folder
                if del_button.area.collidepoint(event.pos):
                    ImageUI.delete_image()
        
            if event.type==pygame.KEYDOWN:
                if event.key==pygame.K_LEFT:
                    # display previous image
                    ImageUI.move_left()
                    pygame.display.update()
                if event.key==pygame.K_RIGHT:
                    # display next image
                    ImageUI.move_right()
                    pygame.display.update()
                if event.key ==pygame.K_SPACE:
                    # move current image to best folder
                    ImageUI.move_image()
                if event.key==pygame.K_BACKSPACE:
                    # delete current image from best folder
                    ImageUI.delete_image()
            
        pygame.display.update()

