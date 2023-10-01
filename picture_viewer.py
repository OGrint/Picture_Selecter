import os
from PIL import Image
import pygame
import shutil

from buttons_and_more import Button

# create directories and list relevant files
def prepare_image_opener(f):
    files=os.listdir(f)

    # create best directory if one doesn't exist
    if 'Best' in files:
        pass
    else:
        os.mkdir(f+'/Best')

    #list all image files in best
    best_files=os.listdir(f+'/Best')
    best_pics=[]
    for i in best_files:
        name=i.split('.')[0]
        if i.endswith('.NEF'):
            best_pics.append(name)
        if i.endswith('.PNG'):
            best_pics.append(name)
        if i.endswith('.JPG'):
            best_pics.append(name)
        if i.endswith('.nef'):
            best_pics.append(name)
        if i.endswith('.png'):
            best_pics.append(name)
        if i.endswith('.jpg'):
            best_pics.append(name)

    # return the files in the best folder
    return(best_pics)

class ImageOpener():
    # make the image opener that will open, handle and cycle through images
    def __init__(self,f,jpg_details,nef_details,jpg_not_nef,centre_w,centre_h,screen,background,width,height):
        self.jpg_details=jpg_details
        self.nef_details=nef_details
        self.position=0
        self.max_pics=len(jpg_details)-1
        # prepare the directory
        self.best_pictures=prepare_image_opener(f)
        self.centre_w=centre_w
        self.centre_h=centre_h
        self.screen=screen
        self.screen_width=width
        self.screen_height=height
        self.image_screen_height=height-110
        self.background=background
        self.f=f
        self.jpg_not_nef=jpg_not_nef
        self.open_image()


    def move_right(self):
        # to move right change position by 1 and open the new image
        # make it so you cant go more right than the final image
        if self.position == self.max_pics:
            pass
        else:
            self.position += 1
            self.open_image()

    def move_left(self):
        # to move left change position by 1 and open the new image
        # make it so you cant go more left than the final image
        if self.position == 0:
            pass
        else:
            self.position -= 1
            self.open_image()

    def open_image(self):


        # load in blank space
        best_button=Button(35,'Black','',self.centre_w,self.centre_h-(self.screen_height/2-20),100,38,self.background,self.screen)
        best_button.draw_box(23,7)
        print()

        # load the image from its location
        image_=list(self.jpg_details.keys())[self.position]
        name=image_+'.jpg'
        location=self.jpg_details[image_]
        im=Image.open(location+'/'+name)
        
        # calculate the size of the image
        width_i,height_i=im.size

        # calculate the ratio of sizes of the image and of the screen
        hw_ratio=height_i/width_i
        r=self.screen_width/self.image_screen_height

        # if landscape resize image to fit based on width
        if hw_ratio >r:
            width_r=width_i/self.screen_width
            new_image_height=height_i/width_r
            new_image_width=width_i/width_r
        # if portrait resize image to fit based on height
        elif hw_ratio<=r:
            height_r=height_i/self.image_screen_height
            new_image_height=height_i/height_r
            new_image_width=width_i/height_r

        # transform the image to new size
        image = pygame.image.load(location+'/'+name).convert()
        image=pygame.transform.scale(image, (new_image_width, new_image_height))

        if len(self.best_pictures)>0:
            if image_ in self.best_pictures:
                # load in the best indicator button as green if in best folder
                best_button=Button(35,'Black','Best',self.centre_w,self.centre_h-(self.screen_height/2-63),100,38,'Green',self.screen)
                best_button.draw_box(23,7)
            else:
                # load in the best indicator button as red if not in best folder
                best_button=Button(35,'Black','Best',self.centre_w,self.centre_h-(self.screen_height/2-63),100,38,'Red',self.screen)
                best_button.draw_box(23,7)

        else:
            # load in the best indicator button
                best_button=Button(35,'Black','Best',self.centre_w,self.centre_h-(self.screen_height/2-63),100,38,'Red',self.screen)
                best_button.draw_box(23,7)

        # find the centre point of the image display area
        image_area_centre_h=self.image_screen_height/2+95
        half_image_w=new_image_width/2
        half_image_h=new_image_height/2

        # load in the name display box
        name_box=Button(32,'Black',image_,self.centre_w+50,self.centre_h-(self.screen_height/2-18),300,40,'Light Grey',self.screen)
        name_box.draw_box(23,9)

        # wipe the old image
        wipe_box=Button(32,'Black','',self.centre_w,image_area_centre_h,10000,598,self.background,self.screen)
        wipe_box.draw_box(23,9)

        # load the image in
        self.screen.blit(image,(self.centre_w-half_image_w,image_area_centre_h-half_image_h))
        pygame.display.flip()

    def move_image(self):
        # copy the current image into the best folder

        # find the current image name
        image_=list(self.jpg_details.keys())[self.position]
        # if the image jpeg and no corresponding nef
        if image_ in self.jpg_not_nef:
            # pull image location
            jpg_location=self.jpg_details[image_]
            # if image already in best pictures pass
            if len(self.best_pictures)>0:

                if image_ in self.best_pictures:
                    pass
                else:
                    shutil.copyfile(jpg_location+'/'+image_+'.jpg', self.f+'/Best/'+image_+'.jpg')
                    self.best_pictures.append(image_)
            # else copy into the best folder
            else:
                shutil.copyfile(jpg_location+'/'+image_+'.jpg', self.f+'/Best/'+image_+'.jpg')
                self.best_pictures.append(image_)
        # if image has corresponding nef copy using nef location dictionary
        else:     
            nef_location=self.nef_details[image_]
            if len(self.best_pictures)>0:
                if image_ in self.best_pictures:
                    pass
                else:
                    shutil.copyfile(nef_location+'/'+image_+'.NEF', self.f+'/Best/'+image_+'.NEF')
                    self.best_pictures.append(image_)
            else:
                shutil.copyfile(nef_location+'/'+image_+'.NEF', self.f+'/Best/'+image_+'.NEF')
                self.best_pictures.append(image_)

        # load in the best indicator button
        best_button=Button(35,'Black','Best',self.centre_w,self.centre_h-(self.screen_height/2-63),100,38,'Green',self.screen)
        best_button.draw_box(23,7)

        

    def delete_image(self):
        # remove current image from best folder
        # find current image
        image_=list(self.jpg_details.keys())[self.position]
        # if image is a jpeg in best
        if image_ in self.jpg_not_nef:
            # remove picture from best
            if image_ in self.best_pictures:
                os.remove(self.f+'/Best/'+image_+'.jpg')
                self.best_pictures.remove(image_)
            else:
                pass
        else:
            # if image an nef in best
            if image_ in self.best_pictures:
                # remove image from best
                os.remove(self.f+'/Best/'+image_+'.NEF')
                self.best_pictures.remove(image_)
            else:
                pass
        
        # load in the best indicator button red to show now not in folder 
        best_button=Button(35,'Black','Best',self.centre_w,self.centre_h-(self.screen_height/2-63),100,38,'Red',self.screen)
        best_button.draw_box(23,7)
        


    
   
