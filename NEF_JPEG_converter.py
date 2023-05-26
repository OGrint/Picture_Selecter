from PIL import Image
import rawpy
import imageio
import pygame


from buttons_and_more import Button
from buttons_and_more import prompt_file
from buttons_and_more import wipe_screen

class Processor():
    # make the image opener that will open, handle and cycle through images
    def __init__(self,f,centre_w,centre_h,screen,background,nef_no_jpeg_list):
        self.centre_w=centre_w
        self.f=f
        self.centre_h=centre_h
        self.screen=screen
        self.screen_width=1910
        self.screen_height=820
        self.background=background
        self.nef_no_jpeg_list=nef_no_jpeg_list
        self.convert_nef_to_jpeg(self.f,self.nef_no_jpeg_list)

    def convert_nef_to_jpeg(self,f,nef_no_jpeg_list):
        # loop through the items in the list of nefs without jpegs
        for n in range(0,len(nef_no_jpeg_list)):
            i = nef_no_jpeg_list[n]
            # open the raw image file
            with rawpy.imread(f+'/'+i+'.nef') as raw:
                # convert and save as jpg
                rgb = raw.postprocess(rawpy.Params(use_camera_wb=True))
                imageio.imsave(f+'/jpegs_temp/'+i+'.jpg',rgb)

                # load in a loading message
            loading_button=Button(32,'Black','Loading... Please Wait',self.centre_w,self.centre_h,300,50,self.background,self.screen)
            loading_button.draw_box(30,13)
            loading_button=Button(32,'Black','',self.centre_w,self.centre_h+50,500,40,'Black',self.screen)
            loading_button.draw_box(40,13)
            loading_button=Button(32,'Black','',self.centre_w-120,self.centre_h+50,243,30,self.background,self.screen)
            loading_button.draw_box(40,13)
            loading_button=Button(32,'Black',f'{n} out of {len(nef_no_jpeg_list)} processed',self.centre_w,self.centre_h+100,300,50,self.background,self.screen)
            loading_button.draw_box(40,13)

            pygame.display.update()

    
        